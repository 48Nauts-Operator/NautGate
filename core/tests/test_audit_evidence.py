import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.audit_evidence import (
    EvidenceFormatError,
    canonical_json,
    checkpoint_payload,
    merkle_root,
    receipt_hash,
)

FIXTURE = Path(__file__).parents[2] / "docs" / "fixtures" / "audit-v1" / "vectors.json"


def _vectors():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _reference_root(digests: list[bytes]) -> bytes:
    """Independent verifier-style implementation for cross-checking fixtures."""
    leaf_prefix = b"NAUTGATE-MERKLE-LEAF-V1\0"
    node_prefix = b"NAUTGATE-MERKLE-NODE-V1\0"
    nodes = [hashlib.sha256(leaf_prefix + digest).digest() for digest in digests]
    while len(nodes) > 1:
        pairs = []
        for offset in range(0, len(nodes), 2):
            if offset + 1 >= len(nodes):
                pairs.append(nodes[offset])
            else:
                pairs.append(
                    hashlib.sha256(node_prefix + nodes[offset] + nodes[offset + 1]).digest()
                )
        nodes = pairs
    return nodes[0]


def test_canonicalization_vector():
    vector = _vectors()["canonicalization"]
    assert canonical_json(vector["value"]).decode() == vector["canonical"]


def test_receipt_hash_vectors():
    for vector in _vectors()["receipts"]:
        assert receipt_hash(vector["value"]).hex() == vector["receipt_hash"]


def test_merkle_vectors_and_independent_implementation():
    receipt_digests = [receipt_hash(v["value"]) for v in _vectors()["receipts"]]
    for vector in _vectors()["merkle"]:
        selected = receipt_digests[: vector["leaf_count"]]
        assert merkle_root(selected).hex() == vector["root"]
        assert _reference_root(selected).hex() == vector["root"]


def test_checkpoint_payload_vector():
    vector = _vectors()["checkpoint"]
    assert checkpoint_payload(vector["value"]).hex() == vector["payload_hex"]


def test_mutated_receipt_has_a_different_hash():
    receipt = _vectors()["receipts"][0]["value"]
    changed = copy.deepcopy(receipt)
    changed["status"] = "error"
    assert receipt_hash(changed) != receipt_hash(receipt)


@pytest.mark.parametrize("value", [1.5, float("nan"), 2**53, b"bytes", {1: "not a string key"}])
def test_ambiguous_or_unsupported_values_fail_closed(value):
    with pytest.raises(EvidenceFormatError):
        canonical_json({"value": value})


def test_unknown_schemas_fail_closed():
    receipt = copy.deepcopy(_vectors()["receipts"][0]["value"])
    receipt["schema"] = "dev.nautgate.decision-receipt/v2"
    with pytest.raises(EvidenceFormatError):
        receipt_hash(receipt)


def test_empty_merkle_tree_fails_closed():
    with pytest.raises(EvidenceFormatError):
        merkle_root([])
