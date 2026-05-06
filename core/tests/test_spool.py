"""Day 4d — durable-spool fallback for route_outcomes.

Tests the OutcomeSpool primitive directly + persist_outcome's DB-fail-then-spool
behavior. The integration with the route handler is indirectly exercised by
test_chat_completions (which monkeypatches queries.write_outcome to record calls).
"""

import json
import uuid
from pathlib import Path

import pytest

from app.outcome import persist_outcome
from app.spool import OutcomeSpool


def _spool(tmp_path: Path) -> OutcomeSpool:
    return OutcomeSpool(tmp_path / "outcomes.ndjson")


# --- OutcomeSpool primitive -----------------------------------------------


def test_append_writes_one_line_per_record(tmp_path):
    sp = _spool(tmp_path)
    sp.append({"decision_id": uuid.uuid4(), "status_code": 200, "duration_ms": 42})
    sp.append({"decision_id": uuid.uuid4(), "status_code": 502, "duration_ms": 100})
    contents = sp.path.read_text().splitlines()
    assert len(contents) == 2
    for line in contents:
        d = json.loads(line)
        assert "decision_id" in d
        assert isinstance(d["decision_id"], str)  # UUID serialized as string


def test_is_empty(tmp_path):
    sp = _spool(tmp_path)
    assert sp.is_empty() is True
    sp.append({"x": 1})
    assert sp.is_empty() is False


@pytest.mark.asyncio
async def test_drain_replays_all_lines_and_unlinks(tmp_path):
    sp = _spool(tmp_path)
    ids = [uuid.uuid4() for _ in range(3)]
    for i in ids:
        sp.append({"decision_id": i, "status_code": 200, "duration_ms": 10})

    seen: list[dict] = []

    async def fake_write(pool, **kw):
        seen.append(kw)

    result = await sp.drain(fake_write, pool=None)
    assert result.drained == 3
    assert result.pending == 0
    assert sp.is_empty() is True  # file removed
    # decision_id was coerced back to UUID
    assert all(isinstance(s["decision_id"], uuid.UUID) for s in seen)
    assert {s["decision_id"] for s in seen} == set(ids)


@pytest.mark.asyncio
async def test_drain_keeps_unprocessed_after_db_failure(tmp_path):
    sp = _spool(tmp_path)
    for i in range(5):
        sp.append({"decision_id": uuid.uuid4(), "status_code": 200, "duration_ms": i})

    fail_after = 2
    call_count = 0

    async def flaky_write(pool, **kw):
        nonlocal call_count
        call_count += 1
        if call_count > fail_after:
            raise ConnectionError("simulated db down")

    result = await sp.drain(flaky_write, pool=None)
    assert result.drained == fail_after
    # The failing line + everything after it stays in the spool for next attempt.
    assert result.pending == 5 - fail_after
    surviving = sp.path.read_text().splitlines()
    assert len(surviving) == 5 - fail_after


@pytest.mark.asyncio
async def test_drain_moves_corrupt_lines_to_bad(tmp_path):
    sp = _spool(tmp_path)
    sp.append({"decision_id": uuid.uuid4(), "status_code": 200, "duration_ms": 1})
    # Inject a corrupt line.
    with sp.path.open("a") as f:
        f.write("{ this is not json\n")
    sp.append({"decision_id": uuid.uuid4(), "status_code": 200, "duration_ms": 2})

    seen = []

    async def fake_write(pool, **kw):
        seen.append(kw)

    result = await sp.drain(fake_write, pool=None)
    assert result.drained == 2
    assert result.skipped_bad == 1
    assert sp.bad_path.exists()
    assert "this is not json" in sp.bad_path.read_text()


# --- persist_outcome wrapper ---------------------------------------------


@pytest.mark.asyncio
async def test_persist_outcome_skips_spool_on_db_success(tmp_path, monkeypatch):
    sp = _spool(tmp_path)
    seen = []

    async def fake_write(pool, **kw):
        seen.append(kw)

    monkeypatch.setattr("app.outcome.queries.write_outcome", fake_write)
    await persist_outcome(
        pool="any", spool=sp, decision_id=uuid.uuid4(), status_code=200, duration_ms=1
    )
    assert len(seen) == 1
    assert sp.is_empty() is True


@pytest.mark.asyncio
async def test_persist_outcome_falls_through_to_spool_on_db_failure(tmp_path, monkeypatch):
    sp = _spool(tmp_path)

    async def boom(pool, **kw):
        raise ConnectionError("db down")

    monkeypatch.setattr("app.outcome.queries.write_outcome", boom)
    did = uuid.uuid4()
    await persist_outcome(pool="any", spool=sp, decision_id=did, status_code=200, duration_ms=1)
    assert sp.is_empty() is False
    line = sp.path.read_text().strip()
    parsed = json.loads(line)
    assert parsed["decision_id"] == str(did)
    assert parsed["status_code"] == 200


@pytest.mark.asyncio
async def test_persist_outcome_with_no_spool_swallows_failure(monkeypatch):
    """No spool configured → log + drop, never raise (don't break the request path)."""

    async def boom(pool, **kw):
        raise ConnectionError("db down")

    monkeypatch.setattr("app.outcome.queries.write_outcome", boom)
    # Should not raise.
    await persist_outcome(pool="any", spool=None, decision_id=uuid.uuid4(), status_code=200)


# --- end-to-end: append → drain → DB receives correct kwargs ------------


@pytest.mark.asyncio
async def test_append_then_drain_round_trip(tmp_path):
    sp = _spool(tmp_path)
    did = uuid.uuid4()
    sp.append(
        {
            "decision_id": did,
            "status_code": 502,
            "duration_ms": 1234,
            "first_byte_ms": None,
            "was_empty": True,
            "was_truncated": False,
            "truncated_at_byte": None,
        }
    )

    received = []

    async def fake_write(pool, **kw):
        received.append(kw)

    result = await sp.drain(fake_write, pool=None)
    assert result.drained == 1
    assert received[0]["decision_id"] == did
    assert received[0]["status_code"] == 502
    assert received[0]["was_empty"] is True
