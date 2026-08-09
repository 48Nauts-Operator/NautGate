"""CLASSIFY slow-path — LLM-confirm sensitivity (Tech Paper §7.3).

When the regex fast-path (app/classify.py) returns "none" but the prompt looks
ambiguous (mentions of "confidential" / "private" / "do not share" / etc.),
we optionally ask a small fast model to classify the text. Off by default;
enable with ``NAUTGATE_CLASSIFY_LLM_CONFIRM=true``.

The LLM verdict can ONLY upgrade fast-path "none" → "pii" or "secret". It
never downgrades. Latency budget: hard 500ms timeout — on any failure we fall
through with the fast-path verdict so the request path stays fast.
"""

from __future__ import annotations

import asyncio

import structlog

from app.classify import Classification

log = structlog.get_logger()

_AMBIGUITY_HINTS: tuple[str, ...] = (
    "private",
    "confidential",
    "internal use",
    "internal-only",
    "do not share",
    "do not distribute",
    "sensitive",
    "restricted",
    "proprietary",
    "trade secret",
    "for your eyes only",
    "nda",
)

_DEFAULT_TIMEOUT_S = 0.5
_MIN_TEXT_LEN = 50  # below this, the call isn't worth the latency
_MAX_TEXT_LEN = 4000  # cap input to keep the LLM call cheap


def is_ambiguous(text: str | None) -> bool:
    """Heuristic gate: True when the text is long enough AND mentions one of the
    ambiguity hint phrases. Cheap to compute — we want to skip the LLM call for
    obvious chitchat.
    """
    if not text or len(text) < _MIN_TEXT_LEN:
        return False
    lower = text.lower()
    return any(h in lower for h in _AMBIGUITY_HINTS)


_PROMPT_TEMPLATE = (
    "You are a sensitivity classifier. Read the text below and reply with EXACTLY "
    "one word on the first line: NONE, PII, or SECRET.\n"
    "  NONE   — no personal data or secrets\n"
    "  PII    — contains personal data (names, emails, addresses, etc.)\n"
    "  SECRET — contains credentials, API keys, or other restricted secrets\n"
    "On the second line, give a brief reason (max 80 chars). No other output.\n\n"
    "TEXT:\n"
)


async def llm_confirm(
    text: str,
    nautrouter,
    *,
    model: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[str, str | None]:
    """Ask an LLM to classify text sensitivity. Returns (sensitivity, reason).

    Always returns ("none", None) on timeout, error, or unparseable output —
    never raises. Sensitivity can only be "none", "pii", or "secret".
    """
    if not text or len(text) < _MIN_TEXT_LEN:
        return "none", None

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT_TEMPLATE + text[:_MAX_TEXT_LEN]}],
        "max_tokens": 60,
        "temperature": 0,
    }

    try:
        async with asyncio.timeout(timeout_s):
            resp = await nautrouter.chat_completions(payload)
    except TimeoutError:
        log.info("classify_llm_timeout", timeout_s=timeout_s)
        return "none", None
    except Exception as exc:
        log.info("classify_llm_error", error=str(exc) or repr(exc), error_type=type(exc).__name__)
        return "none", None

    if not isinstance(resp, dict):
        return "none", None
    try:
        content = (resp["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return "none", None

    if not content:
        return "none", None

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return "none", None
    verdict = lines[0].upper().rstrip(".:")
    reason = lines[1] if len(lines) > 1 else None

    if verdict in ("PII",):
        return "pii", reason
    if verdict in ("SECRET", "RESTRICTED", "CONFIDENTIAL"):
        return "secret", reason
    return "none", None


async def maybe_upgrade_classification(
    classification: Classification,
    *,
    text: str | None,
    nautrouter,
    enabled: bool,
    model: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Classification:
    """Apply the slow-path if ``enabled`` AND fast-path returned "none" AND the text
    is ambiguous. Returns the (possibly upgraded) Classification.
    """
    if not enabled:
        return classification
    if classification.sensitivity != "none":
        return classification  # never downgrade
    if not is_ambiguous(text):
        return classification

    sensitivity, reason = await llm_confirm(
        text or "", nautrouter, model=model, timeout_s=timeout_s
    )
    if sensitivity == "none":
        return classification

    new_signal = {
        "rule_id": "llm_confirm",
        "sensitivity": sensitivity,
        "reason": reason,
    }
    new_reason_parts = [classification.reason] if classification.reason else []
    new_reason_parts.append(f"llm_confirm:{sensitivity}")
    return Classification(
        sensitivity=sensitivity,
        reason=",".join(p for p in new_reason_parts if p),
        signals=[*classification.signals, new_signal],
    )
