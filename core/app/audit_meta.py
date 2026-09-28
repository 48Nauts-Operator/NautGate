"""Per-request audit-metadata extraction.

Surfaces the question "what is my CLI actually shipping when I say hi?":
counts of messages by role, tool definitions, an estimated token-by-role
breakdown so the dashboard can show where the tokens *go* (system / tools
/ history / latest user).

Token estimation is char/4 — same approximation NautRouter uses internally.
Rough but enough to see if a "hi" prompt is actually carrying 12kB of
system + 8 tools + 30 prior turns.
"""

from __future__ import annotations

import json
from typing import Any


def _estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _content_text(content: Any) -> str:
    """Flatten content (string or block list) to plain text for token estimation."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text" and isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
                elif blk.get("type") in ("input_text", "output_text") and isinstance(
                    blk.get("text"), str
                ):
                    parts.append(blk["text"])
        return "\n".join(parts)
    return ""


def extract(payload: dict) -> dict:
    """Extract audit metadata from a (canonical) OpenAI Chat-shaped payload.

    Returns:
        {
            "messages_count": int,
            "tools_count": int,
            "stream_flag": bool,
            "request_size_bytes": int,    # JSON-encoded body length
            "token_estimate": {
                "system": int,       # all system messages
                "tools": int,        # tool / function schemas
                "history": int,      # all non-final user + all assistant messages
                "user": int,         # the LAST user message only
            },
        }

    The token estimate is char/4 — surface intent, not billable accuracy.
    NautRouter / provider usage numbers are the truth for cost; this exists
    so the audit log can show where the tokens *go*.
    """
    messages = payload.get("messages") or []
    tools = payload.get("tools") or payload.get("functions") or []

    if not isinstance(messages, list):
        messages = []
    if not isinstance(tools, list):
        tools = []

    system_text: list[str] = []
    history_text: list[str] = []
    last_user_text = ""
    last_user_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _content_text(m.get("content"))
        if role == "system":
            system_text.append(text)
        elif i == last_user_idx and role == "user":
            last_user_text = text
        else:
            history_text.append(text)

    # Tools: serialize each definition so token-estimate reflects schema bloat.
    tools_text = json.dumps(tools, separators=(",", ":")) if tools else ""

    body_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    return {
        "messages_count": len(messages),
        "tools_count": len(tools),
        "stream_flag": bool(payload.get("stream")),
        "request_size_bytes": body_size,
        "token_estimate": {
            "system": _estimate_tokens("\n".join(system_text)),
            "tools": _estimate_tokens(tools_text),
            "history": _estimate_tokens("\n".join(history_text)),
            "user": _estimate_tokens(last_user_text),
        },
    }


def extract_source(request) -> tuple[str | None, str | None]:
    """Pull source ip + hostname from a FastAPI Request. Hostname is best-effort
    via X-Forwarded-Host / Host headers; if neither is present we leave it None
    rather than block on a reverse DNS lookup in the request path.
    """
    ip = None
    if request.client:
        ip = request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    hostname = (
        request.headers.get("x-naut-source-host")
        or request.headers.get("x-forwarded-host")
        or request.headers.get("host")
    )
    if hostname and ":" in hostname:
        hostname = hostname.split(":", 1)[0]
    return ip, hostname


def audit_health(
    status: dict, *, enabled: bool, lag_warning_s: float, lag_critical_s: float
) -> tuple[str, list[dict]]:
    """Health verdict + alerts for /v1/audit/status.

    The one non-obvious rule (NAUTGATE-72, born from the 11-day silent stall):
    disabled attestation while traffic still flows is an incident, never a calm
    'disabled' badge. Disabled with no traffic stays benign.
    """
    alerts: list[dict] = []
    pending = int(status.get("pending") or 0)
    decisions = int(status.get("decisions_last_hour") or 0)
    receipts = int(status.get("receipts_last_hour") or 0)
    lag = int(status.get("signing_lag_seconds") or 0)

    if status.get("open_gaps"):
        alerts.append({"code": "evidence_gap", "severity": "critical", "count": status["open_gaps"]})
    if status.get("checkpoint_failures"):
        alerts.append(
            {
                "code": "checkpoint_signing_failed",
                "severity": "critical",
                "count": status["checkpoint_failures"],
            }
        )
    if enabled:
        if lag >= lag_critical_s:
            alerts.append({"code": "signing_lag", "severity": "critical", "seconds": lag})
        elif lag >= lag_warning_s:
            alerts.append({"code": "signing_lag", "severity": "warning", "seconds": lag})
        if decisions and receipts < decisions * 0.95:
            alerts.append(
                {
                    "code": "receipt_coverage",
                    "severity": "warning",
                    "decisions_last_hour": decisions,
                    "receipts_last_hour": receipts,
                }
            )
    elif pending or decisions:
        alerts.append(
            {
                "code": "attestation_disabled_with_traffic",
                "severity": "critical",
                "pending": pending,
                "decisions_last_hour": decisions,
            }
        )

    if not enabled and not alerts:
        return "disabled", alerts
    if any(a["severity"] == "critical" for a in alerts):
        return "critical", alerts
    return ("warning", alerts) if alerts else ("healthy", alerts)
