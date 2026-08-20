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
