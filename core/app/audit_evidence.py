"""Normative primitives for NautGate Verified Audit Trail v1.

This module intentionally contains no database, HTTP, or TSB integration.  It
defines the bytes those later components must agree on.  Changing a prefix or
canonicalization rule is a schema-version change, not a refactor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

RECEIPT_SCHEMA = "dev.nautgate.decision-receipt/v1"
CHECKPOINT_SCHEMA = "dev.nautgate.audit-checkpoint/v1"
BUNDLE_SCHEMA = "dev.nautgate.evidence-bundle/v1"

RECEIPT_DOMAIN = b"NAUTGATE-DECISION-RECEIPT-V1\0"
MERKLE_LEAF_DOMAIN = b"NAUTGATE-MERKLE-LEAF-V1\0"
MERKLE_NODE_DOMAIN = b"NAUTGATE-MERKLE-NODE-V1\0"
CHECKPOINT_DOMAIN = b"NAUTGATE-AUDIT-CHECKPOINT-V1\0"

# RFC 8785 interoperable integers are limited to the exact IEEE-754 range.
MAX_SAFE_INTEGER = 2**53 - 1


class EvidenceFormatError(ValueError):
    """The value cannot be represented by the v1 evidence contract."""


def _validate(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
                value.encode("utf-16-be")
            except UnicodeEncodeError as exc:
                raise EvidenceFormatError(f"{path}: strings must contain Unicode scalars") from exc
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise EvidenceFormatError(f"{path}: integer exceeds RFC 8785 safe range")
        return
    if isinstance(value, float):
        raise EvidenceFormatError(f"{path}: floats are forbidden; use an integer unit")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EvidenceFormatError(f"{path}: object keys must be strings")
            _validate(key, f"{path}.<key>")
            _validate(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _validate(child, f"{path}[{index}]")
        return
    raise EvidenceFormatError(f"{path}: unsupported value type {type(value).__name__}")


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        # RFC 8785 sorts property names by UTF-16 code units.
        keys = sorted(value, key=lambda item: item.encode("utf-16-be"))
        return "{" + ",".join(f"{_string(key)}:{_canonical(value[key])}" for key in keys) + "}"
    return "[" + ",".join(_canonical(item) for item in value) + "]"


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON under the strict v1 JCS profile."""
    _validate(value)
    return _canonical(value).encode("utf-8")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def receipt_hash(receipt: Mapping[str, Any]) -> bytes:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise EvidenceFormatError(f"unsupported receipt schema: {receipt.get('schema')!r}")
    return sha256(RECEIPT_DOMAIN + canonical_json(receipt))


def merkle_leaf(receipt_digest: bytes) -> bytes:
    if len(receipt_digest) != 32:
        raise EvidenceFormatError("receipt digest must be exactly 32 bytes")
    return sha256(MERKLE_LEAF_DOMAIN + receipt_digest)


def merkle_parent(left: bytes, right: bytes) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise EvidenceFormatError("Merkle children must be exactly 32 bytes")
    return sha256(MERKLE_NODE_DOMAIN + left + right)


def merkle_root(receipt_digests: Sequence[bytes]) -> bytes:
    """Build the v1 root, promoting an unpaired final node unchanged."""
    if not receipt_digests:
        raise EvidenceFormatError("a Merkle tree requires at least one receipt")
    level = [merkle_leaf(digest) for digest in receipt_digests]
    while len(level) > 1:
        next_level: list[bytes] = []
        for index in range(0, len(level), 2):
            if index + 1 == len(level):
                next_level.append(level[index])
            else:
                next_level.append(merkle_parent(level[index], level[index + 1]))
        level = next_level
    return level[0]


def merkle_proof(receipt_digests: Sequence[bytes], leaf_index: int) -> list[dict[str, str]]:
    """Return the ordered sibling path for one receipt digest."""
    if not receipt_digests:
        raise EvidenceFormatError("a Merkle tree requires at least one receipt")
    if leaf_index < 0 or leaf_index >= len(receipt_digests):
        raise EvidenceFormatError("leaf index is outside the Merkle tree")
    level = [merkle_leaf(digest) for digest in receipt_digests]
    index = leaf_index
    proof: list[dict[str, str]] = []
    while len(level) > 1:
        sibling = index - 1 if index % 2 else index + 1
        if sibling < len(level):
            proof.append(
                {
                    "side": "left" if sibling < index else "right",
                    "hash": level[sibling].hex(),
                }
            )
        next_level: list[bytes] = []
        for offset in range(0, len(level), 2):
            next_level.append(
                level[offset]
                if offset + 1 == len(level)
                else merkle_parent(level[offset], level[offset + 1])
            )
        level = next_level
        index //= 2
    return proof


def verify_merkle_proof(receipt_digest: bytes, proof: Sequence[Mapping[str, str]]) -> bytes:
    """Apply a v1 inclusion proof and return its calculated root."""
    current = merkle_leaf(receipt_digest)
    for item in proof:
        try:
            sibling = bytes.fromhex(item["hash"])
            side = item["side"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceFormatError("invalid Merkle proof item") from exc
        if len(sibling) != 32 or side not in ("left", "right"):
            raise EvidenceFormatError("invalid Merkle proof sibling")
        current = (
            merkle_parent(sibling, current) if side == "left" else merkle_parent(current, sibling)
        )
    return current


def checkpoint_payload(checkpoint: Mapping[str, Any]) -> bytes:
    """Return the exact bytes submitted to TSB for a v1 checkpoint."""
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise EvidenceFormatError(f"unsupported checkpoint schema: {checkpoint.get('schema')!r}")
    return CHECKPOINT_DOMAIN + canonical_json(checkpoint)
