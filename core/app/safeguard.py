"""Structured, content-free evidence for provider safeguard handoffs."""

from __future__ import annotations

from typing import Any

EXTRACTOR_VERSION = "safeguard-v1"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    allowed = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {key: value[key] for key in allowed if isinstance(value.get(key), int)}


def sanitize_iteration(value: Any) -> dict[str, Any] | None:
    """Keep routing/usage facts only; never retain generated or prompt content."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("type", "model", "stop_reason"):
        if text := _text(value.get(key)):
            result[key] = text
    if usage := _usage(value.get("usage")):
        result["usage"] = usage
    details = value.get("stop_details")
    if isinstance(details, dict):
        clean = {
            key: text
            for key in ("type", "category", "recommended_model")
            if (text := _text(details.get(key)))
        }
        if clean:
            result["stop_details"] = clean
    return result or None


def extract_safeguard_evidence(payloads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract provider-confirmed refusal/fallback facts from response payloads."""
    stop_reason = None
    stop_details: dict[str, str] = {}
    served_model = None
    fallback_blocks: list[dict[str, str]] = []
    iterations: list[dict[str, Any]] = []

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        served_model = served_model or _text(payload.get("model"))
        stop_reason = _text(payload.get("stop_reason")) or stop_reason

        message = payload.get("message")
        if isinstance(message, dict):
            served_model = served_model or _text(message.get("model"))
            stop_reason = _text(message.get("stop_reason")) or stop_reason

        delta = payload.get("delta")
        if isinstance(delta, dict):
            stop_reason = _text(delta.get("stop_reason")) or stop_reason
            details_value = delta.get("stop_details")
        else:
            details_value = None
        details_value = payload.get("stop_details") or details_value
        if isinstance(details_value, dict):
            for key in ("type", "category", "recommended_model"):
                if text := _text(details_value.get(key)):
                    stop_details[key] = text

        content = payload.get("content")
        if isinstance(content, list):
            blocks = content
        elif payload.get("type") == "content_block_start":
            blocks = [payload.get("content_block")]
        else:
            blocks = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "fallback":
                continue
            clean = {
                key: text for key in ("from_model", "model") if (text := _text(block.get(key)))
            }
            to_value = block.get("to")
            if isinstance(to_value, dict) and (model := _text(to_value.get("model"))):
                clean["to_model"] = model
            if clean and clean not in fallback_blocks:
                fallback_blocks.append(clean)

        usage = payload.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("iterations"), list):
            for item in usage["iterations"]:
                if clean := sanitize_iteration(item):
                    iterations.append(clean)

    confirmed = (
        stop_reason == "refusal"
        or bool(fallback_blocks)
        or any(
            item.get("type") == "fallback_message" or item.get("stop_reason") == "refusal"
            for item in iterations
        )
    )
    if not confirmed:
        return None
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "evidence_level": "provider_confirmed",
        "stop_reason": stop_reason,
        "stop_details": stop_details or None,
        "served_model": served_model,
        "fallback_blocks": fallback_blocks,
        "usage_iterations": iterations,
    }
