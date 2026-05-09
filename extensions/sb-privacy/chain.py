"""Hash-chain helpers for privacy_log.

Each row's `this_hash` is computed from the previous row's `this_hash`
plus the row's salient fields. A consumer validates the chain by walking
rows in id order: for each row, recompute this_hash from (prev_hash +
payload_hash + ts + decision_id + agent_id + sensitivity) and compare.
Any tamper anywhere in the chain breaks every subsequent row.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

GENESIS_HASH = "0" * 64


def hash_payload(text: str) -> str:
    """Stable hash of a body/excerpt for the audit row."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def link_hash(
    *,
    prev_hash: str,
    payload_hash: str,
    ts: datetime,
    decision_id: UUID | str | None,
    agent_id: str,
    sensitivity: str,
) -> str:
    """Compute this_hash from the previous row's hash and this row's salient fields."""
    parts = [
        prev_hash,
        payload_hash,
        ts.isoformat() if isinstance(ts, datetime) else str(ts),
        str(decision_id) if decision_id is not None else "",
        agent_id,
        sensitivity,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def verify_chain(rows: list[dict]) -> tuple[bool, int | None]:
    """Walk an ordered list of privacy_log rows; return (ok, broken_row_id_or_None).

    Each row should have keys: id, ts (datetime|str), decision_id, agent_id,
    sensitivity, payload_hash, prev_hash, this_hash.
    """
    expected_prev = GENESIS_HASH
    for row in rows:
        if row["prev_hash"] != expected_prev:
            return False, row["id"]
        recomputed = link_hash(
            prev_hash=row["prev_hash"],
            payload_hash=row["payload_hash"],
            ts=row["ts"],
            decision_id=row.get("decision_id"),
            agent_id=row["agent_id"],
            sensitivity=row["sensitivity"],
        )
        if recomputed != row["this_hash"]:
            return False, row["id"]
        expected_prev = row["this_hash"]
    return True, None
