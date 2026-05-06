"""Day 4b — regex-fast-path sensitivity classifier (Tech Paper §7.3).

Pure-function classifier over the assembled user text. Output:
  sensitivity ∈ {"none", "pii", "secret"}, with secret > pii > none precedence.

Day 4c will gate body capture on this signal: secret → metadata only,
pii → capture but redact matched spans, none → capture full body.

This is intentionally the cheap fast-path. The slow-path LLM-confirm pass that the
Tech Paper §7.3 also describes lands later when we have a budget for an extra hop.
"""

import re
from dataclasses import dataclass

# Rules are baked in for v1; move to config/sensitivity_rules.yaml when ops needs to tweak.
# Each entry: (rule_id, sensitivity, compiled_regex). Order doesn't affect correctness —
# precedence is purely max(sensitivity over all matches).
_PII_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
    ("phone_us", re.compile(r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}\b")),
    ("ssn_us", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card_like", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
]

_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-api[0-9]{2}-[A-Za-z0-9_\-]{32,}\b")),
    ("github_pat", re.compile(r"\bgh[psroua]_[A-Za-z0-9]{36,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("nautgate_token", re.compile(r"\bng_[a-f0-9]{32}_[A-Za-z0-9_\-]{40,}\b")),
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


def classify(text: str | None) -> Classification:
    """Run the fast-path regex rules against `text`. Empty/None text → none."""
    if not text:
        return Classification(sensitivity="none", reason=None, signals=[])

    signals: list[dict] = []
    worst = "none"

    for rule_id, pattern in _SECRET_RULES:
        matches = pattern.findall(text)
        if matches:
            signals.append({"rule_id": rule_id, "sensitivity": "secret", "count": len(matches)})
            worst = "secret"

    if worst != "secret":
        for rule_id, pattern in _PII_RULES:
            matches = pattern.findall(text)
            if matches:
                signals.append({"rule_id": rule_id, "sensitivity": "pii", "count": len(matches)})
                if _RANK["pii"] > _RANK[worst]:
                    worst = "pii"

    if worst != "none":
        # Even at "secret" we still want PII signals visible for downstream redaction (Day 4c).
        for rule_id, pattern in _PII_RULES:
            if any(s["rule_id"] == rule_id for s in signals):
                continue
            matches = pattern.findall(text)
            if matches:
                signals.append({"rule_id": rule_id, "sensitivity": "pii", "count": len(matches)})

    reason = ",".join(s["rule_id"] for s in signals) if signals else None
    return Classification(sensitivity=worst, reason=reason, signals=signals)


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
