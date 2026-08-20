from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.audit_evidence import canonical_json, receipt_hash
from app.audit_receipt import build_receipt, content_hash, finalized_receipt
from app.db import queries

ZERO = "0" * 64


def _decision():
    return {
        "id": UUID("00000000-0000-7000-8000-000000000001"),
        "ts": datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        "agent_id": "enga",
        "inbound_format": "anthropic",
        "model_requested": "claude-opus-5",
        "classified_sensitivity": "secret",
        "classified_signals": [{"rule_id": "api-key"}],
        "decision_provider": "nautgate",
        "decision_model": "claude-opus-5",
        "decision_reason": "explicit:claude-opus-5",
        "fallback_chain": [],
        "stream_flag": True,
    }


def _outcome():
    return {
        "ts": datetime(2026, 8, 20, 10, 0, 4, tzinfo=UTC),
        "status_code": 200,
        "prompt_tokens": 1200,
        "completion_tokens": 350,
        "cost_usd": Decimal("0.012345"),
        "actual_provider": "anthropic",
        "actual_model": "claude-opus-5",
        "tool_calls_made": [{"id": "call-1", "name": "read_file"}],
    }


def test_builds_complete_receipt_from_observed_facts(monkeypatch):
    monkeypatch.setenv("NAUTGATE_VERSION", "0.3.0-test")
    from app.version import get_version

    get_version.cache_clear()
    receipt = build_receipt(
        sequence=42,
        receipt_id="00000000-0000-7000-8000-000000000042",
        decision=_decision(),
        outcome=_outcome(),
        evidence={
            "body_sha256": ZERO,
            "upstream_body_sha256": "1" * 64,
            "prompt_sha256": "2" * 64,
            "tools_sha256": "3" * 64,
            "response_sha256": "4" * 64,
            "nautgate_key_id": "ng_ab12",
            "instance_id": "test-instance",
        },
    )
    assert receipt["sequence"] == 42
    assert receipt["client"]["nautgate_key_id"] == "ng_ab12"
    assert receipt["routing"]["observed_model"] == "claude-opus-5"
    assert receipt["result"]["cost_microusd"] == 12345
    assert receipt["tool_evidence"]["calls_observed"] == 1
    assert receipt["runtime"]["nautgate_version"] == "0.3.0-test"
    canonical_json(receipt)  # strict profile accepts the complete result
    get_version.cache_clear()


def test_failure_is_an_evidence_result_not_missing_receipt():
    outcome = _outcome()
    outcome.update(status_code=400, actual_provider=None, actual_model=None, cost_usd=None)
    receipt = build_receipt(sequence=1, decision=_decision(), outcome=outcome)
    assert receipt["result"]["status"] == "error"
    assert receipt["result"]["upstream_status"] == 400
    assert receipt["routing"]["observed_model"] is None


def test_provider_observed_model_difference_is_a_substitution():
    outcome = _outcome()
    outcome["actual_model"] = "claude-opus-5-20260820"
    receipt = build_receipt(sequence=1, decision=_decision(), outcome=outcome)
    assert receipt["routing"]["selected_model"] == "claude-opus-5"
    assert receipt["routing"]["observed_model"] == "claude-opus-5-20260820"
    assert receipt["routing"]["substituted"] is True


def test_finalized_receipt_returns_the_signed_material():
    receipt, encoded, digest = finalized_receipt(
        sequence=1,
        receipt_id="00000000-0000-7000-8000-000000000001",
        decision=_decision(),
        outcome=_outcome(),
    )
    assert encoded == canonical_json(receipt)
    assert digest == receipt_hash(receipt)
    assert len(digest) == 32


def test_content_hash_distinguishes_bytes_text_and_structured_values():
    assert content_hash(b"hello") == content_hash("hello")
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})
    assert content_hash(None) is None


@pytest.mark.asyncio
async def test_outcome_receipt_and_outbox_share_one_transaction():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[{"ts": _outcome()["ts"]}, _decision()])
    conn.fetchval = AsyncMock(return_value=42)
    conn.execute = AsyncMock()
    transaction = conn.transaction.return_value
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    acquired = pool.acquire.return_value
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=None)

    await queries.write_outcome(
        pool,
        decision_id=_decision()["id"],
        status_code=200,
        duration_ms=4000,
        prompt_tokens=1200,
        completion_tokens=350,
        cost_usd=Decimal("0.012345"),
        actual_provider="anthropic",
        actual_model="claude-opus-5",
        evidence={"body_sha256": ZERO, "response_sha256": "4" * 64},
    )

    transaction.__aenter__.assert_awaited_once()
    transaction.__aexit__.assert_awaited_once()
    assert conn.fetchrow.await_count == 2  # outcome RETURNING + joined decision
    assert conn.fetchval.await_count == 1  # transactional, gapless evidence sequence
    assert "UPDATE nautgate.audit_state" in conn.fetchval.await_args.args[0]
    assert conn.execute.await_count == 2  # receipt + outbox
    receipt_insert = conn.execute.await_args_list[0].args
    assert "INSERT INTO nautgate.audit_receipts" in receipt_insert[0]
    assert receipt_insert[3] == 42
    assert (
        bytes(receipt_insert[7]).hex()
        == receipt_hash(__import__("json").loads(receipt_insert[5])).hex()
    )
    assert "INSERT INTO nautgate.audit_outbox" in conn.execute.await_args_list[1].args[0]
