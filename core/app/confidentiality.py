"""Confidentiality classification and local-only routing policy."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.classify import Classification
from app.classify import classify as core_classify

_RANK = {"none": 0, "pii": 1, "secret": 2}
_DECLARATIONS = {
    "none": "none",
    "public": "none",
    "internal": "pii",
    "personal": "pii",
    "pii": "pii",
    "confidential": "secret",
    "secret": "secret",
    "restricted": "secret",
}


class ConfidentialityPolicyError(ValueError):
    """Invalid declaration or unsafe/incomplete routing policy."""


@dataclass(frozen=True)
class ConfidentialityResult:
    classification: Classification
    sources: tuple[str, ...]
    declared: str | None
    bowden_labels: dict[str, int]


def classify_confidentiality(
    base: Classification,
    text: str,
    *,
    declaration: str | None = None,
    bowden_enabled: bool = True,
) -> ConfidentialityResult:
    """Take the strictest of NautGate, Bowden, and the caller declaration.

    Caller input may only upgrade the effective class; it can never suppress a
    detector finding. Bowden raw values are discarded inside this function.
    """
    detected = core_classify(text) if text else Classification("none", None, [])
    sensitivity = max((base.sensitivity, detected.sensitivity), key=_RANK.__getitem__)
    signals = list(base.signals)
    known_rules = {str(item.get("rule_id")) for item in signals}
    signals.extend(item for item in detected.signals if str(item.get("rule_id")) not in known_rules)
    sources = ["nautgate"] if sensitivity != "none" else []
    labels: Counter[str] = Counter()

    if bowden_enabled and text:
        from bowden_pii import detect

        detections = detect(text)
        labels.update(item.label for item in detections)
        rules = Counter(item.rule_id for item in detections)
        if rules:
            sources.append("bowden")
            if _RANK["pii"] > _RANK[sensitivity]:
                sensitivity = "pii"
            signals.extend(
                {
                    "rule_id": f"bowden:{rule_id}",
                    "sensitivity": "pii",
                    "severity": "warning",
                    "count": count,
                }
                for rule_id, count in sorted(rules.items())
            )

    normalized_declaration = None
    if declaration is not None and declaration.strip():
        raw = declaration.strip().lower()
        normalized_declaration = _DECLARATIONS.get(raw)
        if normalized_declaration is None:
            choices = ", ".join(sorted(_DECLARATIONS))
            raise ConfidentialityPolicyError(
                f"invalid X-NautGate-Confidentiality value; expected one of: {choices}"
            )
        sources.append("caller")
        if _RANK[normalized_declaration] > _RANK[sensitivity]:
            sensitivity = normalized_declaration
            signals.append(
                {
                    "rule_id": "caller_confidentiality_declaration",
                    "sensitivity": sensitivity,
                    "severity": "info",
                    "count": 1,
                }
            )

    reason = ",".join(str(item["rule_id"]) for item in signals) or None
    return ConfidentialityResult(
        classification=Classification(sensitivity=sensitivity, reason=reason, signals=signals),
        sources=tuple(dict.fromkeys(sources)),
        declared=normalized_declaration,
        bowden_labels=dict(sorted(labels.items())),
    )


def confidential_route_model(sensitivity: str, config: dict) -> str | None:
    """Return the mandatory local model for this class, or None if unrestricted."""
    if not config.get("enabled"):
        return None
    applies = (sensitivity == "pii" and config.get("route_pii", True)) or (
        sensitivity == "secret" and config.get("route_secret", True)
    )
    if not applies:
        return None
    model = str(config.get("local_model") or "").strip()
    if not model:
        raise ConfidentialityPolicyError(
            "confidential routing is enabled but no local model is configured"
        )
    if not model.startswith("lmstudio/") or len(model) <= len("lmstudio/"):
        raise ConfidentialityPolicyError(
            "confidential local model must use the lmstudio/<model> namespace"
        )
    return model


def redact_bowden(text: str) -> str:
    """Redact Bowden spans without retaining or returning their raw values."""
    if not text:
        return text
    from bowden_pii import detect

    detections = sorted(detect(text), key=lambda item: (item.start, item.end))
    if not detections:
        return text
    parts: list[str] = []
    cursor = 0
    for item in detections:
        if item.start < cursor:
            continue
        parts.append(text[cursor : item.start])
        parts.append(f"[bowden-{item.label.lower()}-redacted]")
        cursor = item.end
    parts.append(text[cursor:])
    return "".join(parts)
