"""Day 4c — body-capture policy gate.

Maps classified_sensitivity → capture policy:
    "none"   → full body captured
    "pii"    → body captured with PII spans redacted
    "secret" → NULL body, metadata only

A configurable byte cap prevents storage runaway on large prompts/responses.
Truncation suffix ``\\n[truncated:<N>]`` is appended; the byte position is
also returned so it can be persisted alongside.
"""

import json
import re
from dataclasses import dataclass

from app.classify import _PII_RULES, _SECRET_RULES

# Sensible default; the streaming-tee cap is 8 MB but ledger storage doesn't
# need that much. 256 KB covers the common case (system + ≤ 100 messages).
BODY_CAPTURE_CAP_BYTES_DEFAULT = 256 * 1024

# Wider redaction: include both PII and SECRET regexes so that "pii" outputs
# don't accidentally leak a secret that the rule merge wasn't strong enough
# to upgrade to "secret" upstream. Cheap and defensive.
_REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = _SECRET_RULES + _PII_RULES


@dataclass(frozen=True)
class CapturedBody:
    body: str | None  # None when policy suppresses (sensitivity == "secret")
    truncated_at_byte: int | None  # None unless we hit the cap


def redact(text: str) -> str:
    """Replace every PII/SECRET regex match with ``[<rule_id>-redacted]``."""
    redacted = text
    for rule_id, pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(f"[{rule_id}-redacted]", redacted)
    return redacted


def _truncate(text: str, cap: int) -> tuple[str, int | None]:
    if len(text.encode("utf-8")) <= cap:
        return text, None
    # Encode-truncate-decode safely (no split mid-codepoint).
    raw = text.encode("utf-8")[:cap]
    safe = raw.decode("utf-8", errors="ignore")
    return safe + f"\n[truncated:{len(text.encode('utf-8'))}]", len(safe.encode("utf-8"))


def capture_prompt(
    messages: list[dict] | None,
    sensitivity: str,
    *,
    cap_bytes: int = BODY_CAPTURE_CAP_BYTES_DEFAULT,
) -> CapturedBody:
    """Return the capture-policy-shaped prompt body.

    Body shape: JSON-serialized messages list (so we can round-trip later).
    None when ``sensitivity == "secret"``.
    """
    if sensitivity == "secret":
        return CapturedBody(body=None, truncated_at_byte=None)
    if not messages:
        return CapturedBody(body=None, truncated_at_byte=None)

    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if sensitivity == "pii":
        serialized = redact(serialized)
    body, trunc = _truncate(serialized, cap_bytes)
    return CapturedBody(body=body, truncated_at_byte=trunc)


def capture_response(
    response: object | None,
    sensitivity: str,
    *,
    cap_bytes: int = BODY_CAPTURE_CAP_BYTES_DEFAULT,
) -> CapturedBody:
    """Same policy as capture_prompt, applied to the upstream response.

    `response` is whatever the upstream returned (typically a dict for non-streaming,
    or the assembled assistant text for streaming). None / empty → no capture.
    """
    if sensitivity == "secret" or response is None:
        return CapturedBody(body=None, truncated_at_byte=None)

    if isinstance(response, str):
        text = response
    else:
        text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))

    if not text:
        return CapturedBody(body=None, truncated_at_byte=None)

    if sensitivity == "pii":
        text = redact(text)
    body, trunc = _truncate(text, cap_bytes)
    return CapturedBody(body=body, truncated_at_byte=trunc)
