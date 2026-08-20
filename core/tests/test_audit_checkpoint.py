from datetime import UTC, datetime, timedelta

import pytest

from app.audit_checkpoint import EvidenceGapError, build_checkpoint
from app.audit_evidence import receipt_hash, verify_merkle_proof


def _rows(count=3, *, start=1):
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    rows = []
    for index in range(count):
        receipt = {
            "schema": "dev.nautgate.decision-receipt/v1",
            "receipt_id": f"00000000-0000-7000-8000-{start + index:012d}",
            "sequence": start + index,
        }
        rows.append(
            {
                "evidence_sequence": start + index,
                "receipt_hash": receipt_hash(receipt),
                "created_at": now + timedelta(seconds=index),
            }
        )
    return rows


@pytest.mark.parametrize("count", [1, 2, 3, 8, 9])
def test_every_receipt_has_a_valid_inclusion_proof(count):
    rows = _rows(count)
    checkpoint, payload, checkpoint_hash, proofs = build_checkpoint(
        rows, instance_id="test", signing_key_id="nautgate-attestation-v1"
    )
    assert checkpoint["receipt_count"] == count
    assert checkpoint["first_sequence"] == 1
    assert checkpoint["last_sequence"] == count
    assert payload.startswith(b"NAUTGATE-AUDIT-CHECKPOINT-V1\0")
    assert len(checkpoint_hash) == 32
    for row, proof in zip(rows, proofs, strict=True):
        assert verify_merkle_proof(row["receipt_hash"], proof).hex() == checkpoint["merkle_root"]


def test_checkpoint_id_is_idempotent_for_the_same_batch():
    rows = _rows()
    first = build_checkpoint(rows, instance_id="test", signing_key_id="key-v1")[0]
    second = build_checkpoint(rows, instance_id="test", signing_key_id="key-v1")[0]
    assert first == second


def test_key_rotation_produces_a_distinct_checkpoint_identity():
    rows = _rows()
    first = build_checkpoint(rows, instance_id="test", signing_key_id="key-v1")[0]
    rotated = build_checkpoint(rows, instance_id="test", signing_key_id="key-v2")[0]
    assert first["checkpoint_id"] != rotated["checkpoint_id"]


def test_previous_checkpoint_hash_is_bound_into_next_checkpoint():
    previous = bytes.fromhex("ab" * 32)
    checkpoint = build_checkpoint(
        _rows(),
        instance_id="test",
        signing_key_id="key-v1",
        previous_checkpoint_hash=previous,
    )[0]
    assert checkpoint["previous_checkpoint_sha256"] == previous.hex()


def test_non_contiguous_receipts_fail_as_an_explicit_gap():
    rows = _rows()
    rows[1]["evidence_sequence"] = 4
    with pytest.raises(EvidenceGapError) as exc:
        build_checkpoint(rows, instance_id="test", signing_key_id="key-v1")
    assert exc.value.expected == 2
    assert exc.value.observed == 4


def test_empty_batch_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        build_checkpoint([], instance_id="test", signing_key_id="key-v1")
