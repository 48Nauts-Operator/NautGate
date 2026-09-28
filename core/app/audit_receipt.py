"""Construction of canonical Verified Audit Trail decision receipts."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.audit_evidence import RECEIPT_SCHEMA, canonical_json, receipt_hash
from app.version import get_version


def content_hash(value: Any) -> str | None:
    """Hash bytes exactly, or structured content in deterministic JSON form."""
    if value is None:
        return None
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        # This hash commits to operational content, not a signed artifact.
        # JSON payloads may legitimately contain floats (temperature, top_p),
        # which the stricter evidence-envelope canonicalizer forbids.
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _microusd(value: Decimal | float | str | None) -> int | None:
    if value is None:
        return None
    return int((Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal(1), ROUND_HALF_UP))


def _hash_optional_json(value: Any) -> str | None:
    return content_hash(value) if value else None


def _numstr(value: Any) -> str | None:
    # Sampling knobs arrive as JSON numbers; the strict evidence profile
    # forbids floats, so the receipt commits to their decimal spelling.
    return None if value is None else str(value)


def sampling_evidence(payload: dict) -> dict:
    """Evidence fields for the sampling knobs the client actually sent."""
    return {
        "sampling_temperature": payload.get("temperature"),
        "sampling_top_p": payload.get("top_p"),
        "sampling_seed": payload.get("seed"),
    }


def environment_evidence(headers, *, capture_path: str) -> dict:
    """Evidence fields for the runtime environment the client declared.

    Recorded, not attested: the harness string and sandbox id are what the
    client sent; capture_path is NautGate's own observation of how the call
    reached it (gateway = in-path, ingest = self-reported by a sidecar).
    """
    get = headers.get if hasattr(headers, "get") else dict(headers).get
    return {
        "env_harness": get("user-agent") or None,
        "env_sandbox_id": get("x-nautgate-sandbox-id") or None,
        "env_capture_path": capture_path,
    }


def build_receipt(
    *,
    sequence: int,
    decision: dict,
    outcome: dict,
    evidence: dict | None = None,
    receipt_id: uuid.UUID | str | None = None,
) -> dict:
    """Build a v1 receipt from facts NautGate directly recorded or observed."""
    evidence = evidence or {}
    status_code = int(outcome["status_code"])
    tool_calls = outcome.get("tool_calls_made")
    if isinstance(tool_calls, str):
        tool_calls = json.loads(tool_calls)
    fallback_chain = decision.get("fallback_chain") or []
    fallback_attempts = [
        "/".join(map(str, item)) if isinstance(item, list) else str(item) for item in fallback_chain
    ]
    requested = decision.get("model_requested")
    selected = decision.get("decision_model")
    observed = outcome.get("actual_model")
    return {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": str(receipt_id or uuid.uuid4()),
        "decision_id": str(decision["id"]),
        "sequence": sequence,
        "started_at": _iso(decision["ts"]),
        "completed_at": _iso(outcome["ts"]),
        "client": {
            "agent_id": str(decision["agent_id"]),
            "nautgate_key_id": str(evidence.get("nautgate_key_id") or "unknown"),
            "protocol": str(decision["inbound_format"]),
        },
        "request": {
            "body_sha256": evidence.get("body_sha256"),
            "upstream_body_sha256": evidence.get("upstream_body_sha256"),
            "prompt_sha256": evidence.get("prompt_sha256"),
            "tools_sha256": evidence.get("tools_sha256"),
            "requested_model": str(requested or ""),
            "stream": bool(decision.get("stream_flag")),
            "sampling": {
                "temperature": _numstr(evidence.get("sampling_temperature")),
                "top_p": _numstr(evidence.get("sampling_top_p")),
                "seed": evidence.get("sampling_seed"),
            },
        },
        "classification": {
            "sensitivity": str(decision.get("classified_sensitivity") or "none"),
            "signals_sha256": _hash_optional_json(decision.get("classified_signals")),
            "policy_version": evidence.get("policy_version"),
            "policy_sha256": evidence.get("policy_sha256"),
        },
        "routing": {
            "selected_provider": decision.get("decision_provider"),
            "selected_transport": evidence.get("selected_transport")
            or decision.get("decision_provider"),
            "selected_model": selected,
            "observed_provider": outcome.get("actual_provider"),
            "observed_model": observed,
            "substituted": bool(
                requested not in (None, "auto")
                and (selected != requested or (observed is not None and observed != requested))
            ),
            "fallback_attempts": fallback_attempts,
            "reason_code": str(decision.get("decision_reason") or "unknown"),
        },
        "result": {
            "status": "success" if 200 <= status_code < 300 else "error",
            "upstream_status": status_code,
            "response_sha256": evidence.get("response_sha256"),
            "finish_reason": evidence.get("finish_reason"),
            "input_tokens": outcome.get("prompt_tokens"),
            "output_tokens": outcome.get("completion_tokens"),
            "cost_microusd": _microusd(outcome.get("cost_usd")),
            "error_code": evidence.get("error_code"),
            "provider_fingerprint": evidence.get("provider_fingerprint"),
        },
        "environment": {
            "harness": evidence.get("env_harness"),
            "sandbox_id": evidence.get("env_sandbox_id"),
            "capture_path": str(evidence.get("env_capture_path") or "unknown"),
        },
        "model_integrity": {
            "weights_digest": evidence.get("weights_digest"),
            "digest_source": str(evidence.get("weights_digest_source") or "unresolved"),
            "resolved_at": evidence.get("weights_resolved_at"),
        },
        "tool_evidence": {
            "calls_observed": len(tool_calls or []),
            "executions_observed": int(evidence.get("tool_executions_observed") or 0),
            "events_sha256": _hash_optional_json(tool_calls),
        },
        "runtime": {
            "nautgate_version": get_version(),
            "instance_id": str(evidence.get("instance_id") or "default"),
            "config_sha256": evidence.get("config_sha256"),
            "build_digest": evidence.get("build_digest"),
        },
    }


def finalized_receipt(**kwargs) -> tuple[dict, bytes, bytes]:
    """Return receipt, canonical bytes, and its domain-separated digest."""
    receipt = build_receipt(**kwargs)
    return receipt, canonical_json(receipt), receipt_hash(receipt)
