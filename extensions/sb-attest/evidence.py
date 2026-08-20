"""Strict NautGate Audit Checkpoint v1 validation and canonicalization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

CHECKPOINT_SCHEMA = "dev.nautgate.audit-checkpoint/v1"
CHECKPOINT_DOMAIN = b"NAUTGATE-AUDIT-CHECKPOINT-V1\0"
MAX_SAFE_INTEGER = 2**53 - 1
_FIELDS = {
    "schema",
    "checkpoint_id",
    "instance_id",
    "first_sequence",
    "last_sequence",
    "receipt_count",
    "merkle_algorithm",
    "merkle_root",
    "opened_at",
    "closed_at",
    "previous_checkpoint_sha256",
    "signing_key_id",
}


class CheckpointError(ValueError):
    pass


def _validate_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
                value.encode("utf-16-be")
            except UnicodeEncodeError as exc:
                raise CheckpointError(f"{path}: invalid Unicode scalar") from exc
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CheckpointError(f"{path}: integer outside safe range")
        return
    if isinstance(value, float):
        raise CheckpointError(f"{path}: floats are forbidden")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CheckpointError(f"{path}: object keys must be strings")
            _validate_json(key, f"{path}.<key>")
            _validate_json(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _validate_json(child, f"{path}[{index}]")
        return
    raise CheckpointError(f"{path}: unsupported value")


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
        keys = sorted(value, key=lambda item: item.encode("utf-16-be"))
        return "{" + ",".join(f"{_string(key)}:{_canonical(value[key])}" for key in keys) + "}"
    return "[" + ",".join(_canonical(item) for item in value) + "]"


def canonical_json(value: Any) -> bytes:
    _validate_json(value)
    return _canonical(value).encode("utf-8")


def checkpoint_payload(checkpoint: Mapping[str, Any], *, expected_key: str) -> bytes:
    if set(checkpoint) != _FIELDS:
        missing = sorted(_FIELDS - set(checkpoint))
        extra = sorted(set(checkpoint) - _FIELDS)
        raise CheckpointError(f"checkpoint fields mismatch: missing={missing}, extra={extra}")
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise CheckpointError("unsupported checkpoint schema")
    if checkpoint.get("merkle_algorithm") != "sha256-binary-v1":
        raise CheckpointError("unsupported Merkle algorithm")
    if checkpoint.get("signing_key_id") != expected_key:
        raise CheckpointError("checkpoint signing key does not match configured key")
    first = checkpoint.get("first_sequence")
    last = checkpoint.get("last_sequence")
    count = checkpoint.get("receipt_count")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in (first, last, count)
    ):
        raise CheckpointError("checkpoint range must use integers")
    if first < 1 or last < first or count != last - first + 1:
        raise CheckpointError("checkpoint range and receipt count disagree")
    for field in ("merkle_root", "previous_checkpoint_sha256"):
        value = checkpoint.get(field)
        if value is None and field == "previous_checkpoint_sha256":
            continue
        try:
            raw = bytes.fromhex(value)
        except (TypeError, ValueError):
            raise CheckpointError(f"{field} must be lowercase SHA-256 hex") from None
        if len(raw) != 32 or value != value.lower():
            raise CheckpointError(f"{field} must be lowercase SHA-256 hex")
    return CHECKPOINT_DOMAIN + canonical_json(checkpoint)
