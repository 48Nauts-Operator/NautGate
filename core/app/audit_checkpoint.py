"""Deterministic construction of unsigned Verified Audit Trail checkpoints."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.audit_evidence import (
    CHECKPOINT_SCHEMA,
    checkpoint_payload,
    merkle_proof,
    merkle_root,
)

CHECKPOINT_NAMESPACE = uuid.UUID("f8a232e8-3bd7-4d71-b190-7149fb2a7989")


class EvidenceGapError(RuntimeError):
    def __init__(self, expected: int, observed: int):
        super().__init__(f"evidence sequence gap: expected {expected}, observed {observed}")
        self.expected = expected
        self.observed = observed


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_checkpoint(
    receipts: Sequence[dict[str, Any]],
    *,
    instance_id: str,
    signing_key_id: str,
    previous_checkpoint_hash: bytes | None = None,
) -> tuple[dict, bytes, bytes, list[list[dict[str, str]]]]:
    """Build checkpoint, TSB payload, checkpoint hash, and inclusion proofs."""
    if not receipts:
        raise ValueError("cannot checkpoint an empty receipt batch")
    sequences = [int(row["evidence_sequence"]) for row in receipts]
    expected = list(range(sequences[0], sequences[0] + len(sequences)))
    if sequences != expected:
        mismatch = next(
            i for i, pair in enumerate(zip(expected, sequences, strict=True)) if pair[0] != pair[1]
        )
        raise EvidenceGapError(expected[mismatch], sequences[mismatch])
    digests = [bytes(row["receipt_hash"]) for row in receipts]
    root = merkle_root(digests)
    opened_at = min(row["created_at"] for row in receipts)
    closed_at = max(row["created_at"] for row in receipts)
    previous_hex = previous_checkpoint_hash.hex() if previous_checkpoint_hash else "genesis"
    stable_name = (
        f"{instance_id}:{sequences[0]}:{sequences[-1]}:{root.hex()}:{previous_hex}:{signing_key_id}"
    )
    checkpoint_id = uuid.uuid5(CHECKPOINT_NAMESPACE, stable_name)
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA,
        "checkpoint_id": str(checkpoint_id),
        "instance_id": instance_id,
        "first_sequence": sequences[0],
        "last_sequence": sequences[-1],
        "receipt_count": len(receipts),
        "merkle_algorithm": "sha256-binary-v1",
        "merkle_root": root.hex(),
        "opened_at": _iso(opened_at),
        "closed_at": _iso(closed_at),
        "previous_checkpoint_sha256": (
            previous_checkpoint_hash.hex() if previous_checkpoint_hash else None
        ),
        "signing_key_id": signing_key_id,
    }
    payload = checkpoint_payload(checkpoint)
    checkpoint_hash = hashlib.sha256(payload).digest()
    proofs = [merkle_proof(digests, index) for index in range(len(digests))]
    return checkpoint, payload, checkpoint_hash, proofs
