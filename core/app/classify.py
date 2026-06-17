"""Day 4b — regex-fast-path sensitivity classifier (Tech Paper §7.3).

Pure-function classifier over the assembled user text. Output:
  sensitivity ∈ {"none", "pii", "secret"}, with secret > pii > none precedence.

Day 4c will gate body capture on this signal: secret → metadata only,
pii → capture but redact matched spans, none → capture full body.

This is intentionally the cheap fast-path. The slow-path LLM-confirm pass that the
Tech Paper §7.3 also describes lands later when we have a budget for an extra hop.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.cards import is_valid_card_number
from app.findings import SEVERITY as _SEV

# Per-rule match validators. A regex match for one of these rules only counts
# when its validator returns True — used to suppress the huge false-positive
# rate of the cheap `credit_card_like` digit-run regex (timestamps, IDs, etc.).
_MATCH_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "credit_card_like": is_valid_card_number,
}

# Rules are baked in for v1; move to config/sensitivity_rules.yaml when ops needs to tweak.
# Each entry: (rule_id, sensitivity, compiled_regex). Order doesn't affect correctness —
# precedence is purely max(sensitivity over all matches).
_PII_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
    ("phone_us", re.compile(r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}\b")),
    ("ssn_us", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Candidate shapes only — a real PAN is 13–19 contiguous digits, or digits
    # in regular 4-groups (4-4-4-4[-3]) or Amex 4-6-5. The old "(?:\d[ -]?){13,19}"
    # allowed a separator after EVERY digit, which welded space-separated numeric
    # columns (an `ls -l` size/date/time row) into a fake 16-digit "card". Every
    # candidate is still Luhn+IIN validated by _MATCH_VALIDATORS before it counts.
    ("credit_card_like", re.compile(
        r"\b\d{13,19}\b"
        r"|\b\d{4}(?:[ \-]\d{4}){2,3}(?:[ \-]\d{1,4})?\b"
        r"|\b\d{4}[ \-]\d{6}[ \-]\d{5}\b"
    )),
]

_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Provider API keys (extended via ClawProxy parity).
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-api[0-9]{2}-[A-Za-z0-9_\-]{32,}\b")),
    ("github_pat", re.compile(r"\bgh[psroua]_[A-Za-z0-9]{36,}\b")),
    ("github_personal_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # AWS secret keys are 40 char base64; require an "aws" / "secret" context word
    # to keep false-positive rate sane.
    (
        "aws_secret_key",
        re.compile(
            r"\b(?:aws[_\- ]?secret[_\- ]?(?:access[_\- ]?)?key|secret[_\- ]?access[_\- ]?key)\b[\s:=\"']+([A-Za-z0-9/+]{40})\b",
            re.IGNORECASE,
        ),
    ),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}\b")),
    ("sendgrid_api_key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b")),
    ("twilio_auth_token", re.compile(r"\b(?:SK|AC)[a-f0-9]{32}\b")),
    (
        "azure_connection_string",
        re.compile(
            r"\bDefaultEndpointsProtocol=[^\s'\"]+;AccountKey=[A-Za-z0-9+/=]{20,}", re.IGNORECASE
        ),
    ),
    # Other credentials & secrets.
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("nautgate_token", re.compile(r"\bng_[a-f0-9]{32}_[A-Za-z0-9_\-]{40,}\b")),
    (
        "bearer_token",
        re.compile(r"\b[Bb]earer\s+[A-Za-z0-9_\-.]{20,}\b"),
    ),
    (
        "http_basic_auth_url",
        re.compile(r"\bhttps?://[A-Za-z0-9_.\-]+:[^@\s/]{4,}@[A-Za-z0-9_.\-]+"),
    ),
    (
        "database_url",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"]{10,}",
            re.IGNORECASE,
        ),
    ),
    (
        "env_file_content",
        re.compile(
            r"^[A-Z][A-Z0-9_]{2,}=(?!\s|$)['\"]?[^\s'\"]{6,}",
            re.MULTILINE,
        ),
    ),
    (
        "generic_secret",
        re.compile(
            r"(?:secret|password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?",
            re.IGNORECASE,
        ),
    ),
    (
        "generic_api_key",
        re.compile(
            r"\b(?:api[_\- ]?key|apikey)\b[\s:=\"']+([A-Za-z0-9_\-]{20,})",
            re.IGNORECASE,
        ),
    ),
    # Infrastructure.
    (
        "ip_address_private",
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    (
        "ssh_key_reference",
        re.compile(r"\b(?:id_(?:rsa|ed25519|ecdsa|dsa)(?:\.pub)?|authorized_keys)\b"),
    ),
]

_RANK = {"none": 0, "pii": 1, "secret": 2}


@dataclass(frozen=True)
class Classification:
    sensitivity: str  # "none" | "pii" | "secret"
    reason: str | None  # human-readable, e.g. "github_pat,email"
    signals: list[dict]  # [{rule_id, sensitivity, count}]

    def to_db(self) -> dict:
        """Shape for INSERT into route_decisions (signals → JSONB)."""
        return {
            "sensitivity": self.sensitivity,
            "signals": self.signals or None,
        }


def _validated(rule_id: str, matches: list) -> list:
    """Drop matches that fail the rule's validator (e.g. non-card digit runs)."""
    v = _MATCH_VALIDATORS.get(rule_id)
    if not v:
        return matches
    return [m for m in matches if isinstance(m, str) and v(m)]


def _signal(rule_id: str, sensitivity: str, count: int) -> dict:
    """Standard signal shape — includes Lighthouse-audit severity from findings.SEVERITY."""
    return {
        "rule_id": rule_id,
        "sensitivity": sensitivity,
        "severity": _SEV.get(rule_id, "info"),
        "count": count,
    }


def classify(text: str | None) -> Classification:
    """Run the fast-path regex rules against `text`. Empty/None text → none."""
    if not text:
        return Classification(sensitivity="none", reason=None, signals=[])

    signals: list[dict] = []
    worst = "none"

    for rule_id, pattern in _SECRET_RULES:
        matches = pattern.findall(text)
        if matches:
            signals.append(_signal(rule_id, "secret", len(matches)))
            worst = "secret"

    if worst != "secret":
        for rule_id, pattern in _PII_RULES:
            matches = _validated(rule_id, pattern.findall(text))
            if matches:
                signals.append(_signal(rule_id, "pii", len(matches)))
                if _RANK["pii"] > _RANK[worst]:
                    worst = "pii"

    if worst != "none":
        # Even at "secret" we still want PII signals visible for downstream redaction (Day 4c).
        for rule_id, pattern in _PII_RULES:
            if any(s["rule_id"] == rule_id for s in signals):
                continue
            matches = _validated(rule_id, pattern.findall(text))
            if matches:
                signals.append(_signal(rule_id, "pii", len(matches)))

    reason = ",".join(s["rule_id"] for s in signals) if signals else None
    return Classification(sensitivity=worst, reason=reason, signals=signals)


def scan_for_findings(text: str | None) -> list[dict]:
    """Re-scan a text body for all rules, returning sample-bearing findings.

    Used by the privacy audit endpoint to extract matched_text. Distinct from
    classify() which only returns aggregated counts. Skips PII inside text that
    also contains secrets, since the audit treats them as separate categories.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rule_list, sensitivity in ((_SECRET_RULES, "secret"), (_PII_RULES, "pii")):
        for rule_id, pattern in rule_list:
            validator = _MATCH_VALIDATORS.get(rule_id)
            for m in pattern.finditer(text):
                # If the rule has a capture group (e.g. generic_secret), prefer it
                # so we don't store the surrounding label.
                sample = m.group(1) if m.groups() else m.group(0)
                # Suppress matches that fail the rule's validator (e.g. a digit
                # run that isn't a real card number — timestamps, IDs).
                if validator and not validator(sample):
                    continue
                key = (rule_id, sample[:20])
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "rule_id": rule_id,
                        "sensitivity": sensitivity,
                        "severity": _SEV.get(rule_id, "info"),
                        "matched_text": sample[:80],
                    }
                )
    return out


def assemble_user_text(messages: list[dict] | None) -> str:
    """Concatenate every user message's text content. Used as the classifier input.

    This is the full text — not the 200-char excerpt — because a secret past char 200
    would otherwise slip through the gate.
    """
    if not messages:
        return ""
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    txt = blk.get("text", "")
                    if isinstance(txt, str):
                        parts.append(txt)
    return "\n".join(parts)
