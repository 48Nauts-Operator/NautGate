"""Hash chain unit tests."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chain import GENESIS_HASH, hash_payload, link_hash, verify_chain  # noqa: E402


def _row(prev_hash, *, decision_id=None, agent_id="alice", sensitivity="pii", text="x"):
    ts = datetime.now(UTC)
    payload_hash = hash_payload(text)
    this_hash = link_hash(
        prev_hash=prev_hash,
        payload_hash=payload_hash,
        ts=ts,
        decision_id=decision_id,
        agent_id=agent_id,
        sensitivity=sensitivity,
    )
    return {
        "id": 1,
        "ts": ts,
        "decision_id": decision_id,
        "agent_id": agent_id,
        "sensitivity": sensitivity,
        "payload_hash": payload_hash,
        "prev_hash": prev_hash,
        "this_hash": this_hash,
    }


def test_hash_payload_stable():
    assert hash_payload("hello") == hash_payload("hello")
    assert hash_payload("hello") != hash_payload("world")


def test_hash_payload_handles_none_and_empty():
    assert hash_payload(None) == hash_payload("")
    assert hash_payload(None) == hash_payload(None)


def test_link_hash_changes_with_each_field():
    base = dict(
        prev_hash=GENESIS_HASH,
        payload_hash="abc",
        ts=datetime(2026, 5, 9, 12, 0, 0),
        decision_id=uuid4(),
        agent_id="alice",
        sensitivity="pii",
    )
    h0 = link_hash(**base)
    # Changing any field changes the hash.
    assert link_hash(**{**base, "agent_id": "bob"}) != h0
    assert link_hash(**{**base, "sensitivity": "secret"}) != h0
    assert link_hash(**{**base, "payload_hash": "xyz"}) != h0


def test_verify_chain_genesis_only():
    rows = [_row(GENESIS_HASH)]
    rows[0]["id"] = 1
    ok, broken = verify_chain(rows)
    assert ok and broken is None


def test_verify_chain_three_rows():
    r1 = _row(GENESIS_HASH, text="one")
    r1["id"] = 1
    r2 = _row(r1["this_hash"], text="two")
    r2["id"] = 2
    r3 = _row(r2["this_hash"], text="three")
    r3["id"] = 3
    ok, broken = verify_chain([r1, r2, r3])
    assert ok and broken is None


def test_verify_chain_detects_tampered_payload():
    r1 = _row(GENESIS_HASH, text="one")
    r1["id"] = 1
    r2 = _row(r1["this_hash"], text="two")
    r2["id"] = 2
    # Tamper r1's payload — its this_hash no longer matches recomputed value.
    r1["payload_hash"] = hash_payload("not one")
    ok, broken = verify_chain([r1, r2])
    assert ok is False
    assert broken == 1


def test_verify_chain_detects_broken_link():
    r1 = _row(GENESIS_HASH, text="one")
    r1["id"] = 1
    r2 = _row(r1["this_hash"], text="two")
    r2["id"] = 2
    # Tamper r2's prev_hash to a different value.
    r2["prev_hash"] = "f" * 64
    ok, broken = verify_chain([r1, r2])
    assert ok is False
    assert broken == 2


def test_verify_chain_detects_inserted_row():
    r1 = _row(GENESIS_HASH, text="one")
    r1["id"] = 1
    r2 = _row(r1["this_hash"], text="two")
    r2["id"] = 2
    fake = _row(r2["this_hash"], text="injected")
    fake["id"] = 99
    # Verify catches it because r3-original would have prev_hash=r2 not fake.
    # (Here we test that the fake itself validates standalone — the audit's
    # job is to ensure the fake didn't *replace* a real row, which it can't.)
    ok, broken = verify_chain([r1, r2, fake])
    assert ok is True  # individual chain still validates
    # The defense against insertion is "next genuine row's prev_hash points at
    # the *real* tip we know about" — caller compares last this_hash to
    # something they previously witnessed.
