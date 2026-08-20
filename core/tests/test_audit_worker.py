from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.audit_evidence import receipt_hash
from app.audit_worker import stage_checkpoint_once


def _pool(*, pending, previous, rows):
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[pending, previous])
    conn.fetch = AsyncMock(return_value=rows)
    transaction = conn.transaction.return_value
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    acquired = pool.acquire.return_value
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


def _row(sequence):
    receipt = {
        "schema": "dev.nautgate.decision-receipt/v1",
        "receipt_id": f"00000000-0000-7000-8000-{sequence:012d}",
        "sequence": sequence,
    }
    return {
        "receipt_id": UUID(receipt["receipt_id"]),
        "evidence_sequence": sequence,
        "receipt_hash": receipt_hash(receipt),
        "created_at": datetime.now(UTC) - timedelta(minutes=2),
    }


@pytest.mark.asyncio
async def test_worker_waits_for_young_incomplete_batch():
    pool, conn = _pool(pending={"count": 1, "oldest": datetime.now(UTC)}, previous=None, rows=[])
    result = await stage_checkpoint_once(
        pool,
        instance_id="test",
        signing_key_id="key-v1",
        max_receipts=1000,
        max_age_seconds=60,
    )
    assert result.status == "waiting"
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_stages_contiguous_receipts_atomically():
    rows = [_row(1), _row(2), _row(3)]
    pool, conn = _pool(
        pending={"count": 3, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )
    result = await stage_checkpoint_once(
        pool, instance_id="test", signing_key_id="key-v1", force=True
    )
    assert result.status == "staged"
    assert result.receipt_count == 3
    sql = "\n".join(call.args[0] for call in conn.execute.await_args_list)
    assert "INSERT INTO nautgate.audit_checkpoints" in sql
    assert sql.count("UPDATE nautgate.audit_receipts") == 3
    assert "DELETE FROM nautgate.audit_outbox" in sql


@pytest.mark.asyncio
async def test_checkpoint_timestamps_are_bound_as_datetimes():
    # asyncpg binds by type, so the canonical ISO strings the checkpoint carries
    # are rejected by a timestamptz column and the whole staging transaction
    # fails. Nothing is ever signed, and the only sign of it is a DataError in
    # the log every tick.
    rows = [_row(1)]
    pool, conn = _pool(
        pending={"count": 1, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )
    await stage_checkpoint_once(pool, instance_id="test", signing_key_id="key-v1", force=True)
    insert = next(
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO nautgate.audit_checkpoints" in call.args[0]
    )
    opened_at, closed_at = insert.args[13], insert.args[14]
    assert isinstance(opened_at, datetime) and isinstance(closed_at, datetime)


@pytest.mark.asyncio
async def test_worker_records_gap_and_does_not_stage_checkpoint():
    rows = [_row(3)]
    pool, conn = _pool(
        pending={"count": 1, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )
    result = await stage_checkpoint_once(
        pool, instance_id="test", signing_key_id="key-v1", force=True
    )
    assert result.status == "gap"
    assert result.expected_sequence == 1
    assert result.observed_sequence == 3
    sql = "\n".join(call.args[0] for call in conn.execute.await_args_list)
    assert "INSERT INTO nautgate.audit_gaps" in sql
    assert "INSERT INTO nautgate.audit_checkpoints" not in sql


@pytest.mark.asyncio
async def test_duplicate_worker_delivery_uses_same_checkpoint_identity():
    rows = [_row(1), _row(2)]
    first_pool, first_conn = _pool(
        pending={"count": 2, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )
    second_pool, second_conn = _pool(
        pending={"count": 2, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )
    first = await stage_checkpoint_once(
        first_pool, instance_id="test", signing_key_id="key-v1", force=True
    )
    duplicate = await stage_checkpoint_once(
        second_pool, instance_id="test", signing_key_id="key-v1", force=True
    )
    assert first.checkpoint_id == duplicate.checkpoint_id
    for conn in (first_conn, second_conn):
        insert = next(
            call.args[0]
            for call in conn.execute.await_args_list
            if "INSERT INTO nautgate.audit_checkpoints" in call.args[0]
        )
        assert "ON CONFLICT (checkpoint_id) DO NOTHING" in insert


@pytest.mark.asyncio
async def test_worker_crash_keeps_staging_inside_one_transaction():
    rows = [_row(1), _row(2)]
    pool, conn = _pool(
        pending={"count": 2, "oldest": rows[0]["created_at"]}, previous=None, rows=rows
    )

    async def execute(sql, *_args):
        if "UPDATE nautgate.audit_receipts" in sql:
            raise RuntimeError("worker crashed")

    conn.execute.side_effect = execute
    with pytest.raises(RuntimeError, match="worker crashed"):
        await stage_checkpoint_once(pool, instance_id="test", signing_key_id="key-v1", force=True)
    transaction = conn.transaction.return_value
    assert transaction.__aenter__.await_count == 1
    assert transaction.__aexit__.await_count == 1
    # The outbox delete is after every receipt update and was never reached.
    assert not any(
        "DELETE FROM nautgate.audit_outbox" in call.args[0] for call in conn.execute.await_args_list
    )
