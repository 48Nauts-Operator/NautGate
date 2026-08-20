import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.audit_checkpoint import build_checkpoint
from app.audit_evidence import receipt_hash
from app.audit_signer import sign_checkpoint_once


def _checkpoint_row():
    receipt = {"schema": "dev.nautgate.decision-receipt/v1", "receipt_id": "r1"}
    checkpoint, _, checkpoint_hash, _ = build_checkpoint(
        [
            {
                "evidence_sequence": 1,
                "receipt_hash": receipt_hash(receipt),
                "created_at": datetime(2026, 8, 20, tzinfo=UTC),
            }
        ],
        instance_id="test",
        signing_key_id="key-v1",
    )
    return {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "canonical_checkpoint": checkpoint,
        "checkpoint_hash": checkpoint_hash,
        "key_id": "key-v1",
        "attempt_count": 0,
    }


def _pool(row):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()
    transaction = conn.transaction.return_value
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    acquired = pool.acquire.return_value
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


@pytest.mark.asyncio
async def test_verified_sidecar_response_finalizes_checkpoint_and_receipts():
    row = _checkpoint_row()

    def handler(request):
        assert request.headers["x-nautgate-attest-token"] == "internal-secret"
        return httpx.Response(
            200,
            json={
                "verified": True,
                "checkpoint_id": row["checkpoint_id"],
                "checkpoint_hash": bytes(row["checkpoint_hash"]).hex(),
                "key_id": "key-v1",
                "algorithm": "SHA256_WITH_RSA",
                "encoding": "base64-der",
                "signature": base64.b64encode(b"signature").decode(),
                "public_key_fingerprint": "ab" * 32,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool, conn = _pool(row)
    result = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="internal-secret",
        expected_key_id="key-v1",
        expected_fingerprint="ab" * 32,
        client=client,
    )
    await client.aclose()
    assert result.status == "verified"
    sql = "\n".join(call.args[0] for call in conn.execute.await_args_list)
    assert "UPDATE nautgate.audit_checkpoints" in sql
    assert "UPDATE nautgate.audit_receipts" in sql


@pytest.mark.asyncio
async def test_key_fingerprint_change_is_retried_not_trusted():
    row = _checkpoint_row()
    response = {
        "verified": True,
        "checkpoint_id": row["checkpoint_id"],
        "checkpoint_hash": bytes(row["checkpoint_hash"]).hex(),
        "key_id": "key-v1",
        "algorithm": "SHA256_WITH_RSA",
        "encoding": "base64-der",
        "signature": base64.b64encode(b"signature").decode(),
        "public_key_fingerprint": "changed",
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    )
    pool, conn = _pool(row)
    result = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="secret",
        expected_key_id="key-v1",
        expected_fingerprint="ab" * 32,
        client=client,
    )
    await client.aclose()
    assert result.status == "retry"
    assert "fingerprint" in result.error
    assert "UPDATE nautgate.audit_receipts" not in "\n".join(
        call.args[0] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_signer_is_idle_when_no_checkpoint_is_staged():
    pool, _ = _pool(None)
    result = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="secret",
        expected_key_id="key-v1",
    )
    assert result.status == "empty"


@pytest.mark.asyncio
async def test_tsb_outage_retries_same_checkpoint_then_recovers():
    row = _checkpoint_row()
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": "TSB unavailable"})
        return httpx.Response(
            200,
            json={
                "verified": True,
                "checkpoint_id": row["checkpoint_id"],
                "checkpoint_hash": bytes(row["checkpoint_hash"]).hex(),
                "key_id": "key-v1",
                "algorithm": "SHA256_WITH_RSA",
                "encoding": "base64-der",
                "signature": base64.b64encode(b"signature").decode(),
                "public_key_fingerprint": "ab" * 32,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool, conn = _pool(row)
    first = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="secret",
        expected_key_id="key-v1",
        expected_fingerprint="ab" * 32,
        client=client,
    )
    second = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="secret",
        expected_key_id="key-v1",
        expected_fingerprint="ab" * 32,
        client=client,
    )
    await client.aclose()
    assert first.status == "retry"
    assert second.status == "verified"
    assert first.checkpoint_id == second.checkpoint_id == row["checkpoint_id"]
    receipt_updates = [
        call
        for call in conn.execute.await_args_list
        if "UPDATE nautgate.audit_receipts" in call.args[0]
    ]
    assert len(receipt_updates) == 1


@pytest.mark.asyncio
async def test_exhausted_signing_attempt_is_visible_and_never_verifies_receipts():
    row = _checkpoint_row()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="offline"))
    )
    pool, conn = _pool(row)
    result = await sign_checkpoint_once(
        pool,
        sidecar_url="http://sb-attest:8004",
        internal_token="secret",
        expected_key_id="key-v1",
        max_attempts=1,
        client=client,
    )
    await client.aclose()
    assert result.status == "failed"
    calls = conn.execute.await_args_list
    assert any(call.args[2] == "failed" for call in calls if "status = $2" in call.args[0])
    assert not any("UPDATE nautgate.audit_receipts" in call.args[0] for call in calls)
