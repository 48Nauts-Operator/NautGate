"""Compliance AUDIT layer (NAUTGATE-25).

NautGate is the audit layer for compliance, **not** the compliance layer. Nothing
in this module blocks, denies or holds a call — it only produces the
``compliance_trace`` that gets recorded alongside the audit row, so an operator
can go back later and see what a call actually touched and which rules it may
have tapped into.

Two consequences of that framing, both deliberate:

* The label (G/Y/O/R/X) is a **severity reading** — "how closely does this want
  looking at" — not an enforcement band. A wrong label is a reviewable
  annotation, never blocked work, which is why the classifier does not need to
  be perfect on day one.
* Jurisdiction scope filters the **flags**, never the **recording**. The full
  trace is built for every call whatever is declared, so adding a market later
  re-illuminates existing history instead of having quietly lost it.

Labels are a baseline reading, not a legal opinion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Severity order — used to take the strictest of several readings.
_LABEL_ORDER = {"G": 0, "Y": 1, "O": 2, "R": 3, "X": 4}

# Sensitivity from the existing regex classifier → data class on the trace.
_SENSITIVITY_TO_CLASS = {"none": "public", "pii": "personal", "secret": "secret"}


def strictest(*labels: str) -> str:
    """Return the strictest of the given labels. Unknown values are ignored."""
    known = [x for x in labels if x in _LABEL_ORDER]
    return max(known, key=lambda x: _LABEL_ORDER[x]) if known else "G"


@dataclass(frozen=True)
class Flag:
    id: str
    severity: str  # "critical" | "high"
    regime: str
    title: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "regime": self.regime,
            "title": self.title,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Trace:
    """What one call touched. Recorded; never used to gate."""

    activity: str
    label: str
    confidence: str  # "declared" | "inferred" | "fallback"
    effect: str
    data_class: str
    evaluated_against: list[str]
    regimes_touched: list[str]
    destination: dict
    provider_terms: dict
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "activity": self.activity,
            "label": self.label,
            "confidence": self.confidence,
            "effect": self.effect,
            "data_class": self.data_class,
            "evaluated_against": self.evaluated_against,
            "regimes_touched": self.regimes_touched,
            "destination": self.destination,
            "provider_terms": self.provider_terms,
            "flags": [f.to_dict() for f in self.flags],
        }


class Policy:
    """Loaded compliance.yaml. Read-only after construction."""

    def __init__(self, raw: dict[str, Any]):
        self._raw = raw or {}
        self.scope = self._raw.get("scope") or {}
        self.regimes = self._raw.get("regimes") or {}
        self.sector_regimes = self._raw.get("sector_regimes") or {}
        self.home_regions = set(self._raw.get("home_regions") or [])
        self.providers = self._raw.get("providers") or {}
        self.activities = self._raw.get("activities") or {}
        self.unknown_activity = self._raw.get("unknown_activity") or {
            "label": "O",
            "effect": "recommendation",
        }
        self.flag_rules = {f["id"]: f for f in (self._raw.get("flags") or []) if "id" in f}

    # ---- scope ----------------------------------------------------------

    def evaluated_against(self) -> list[str]:
        """The regimes this deployment reads traffic against.

        Establishment ∪ markets ∪ sector overlays. Recorded on every trace so the
        lens itself is auditable — you can always tell which law a past reading
        was made under.
        """
        out: list[str] = []
        keys = [self.scope.get("establishment")] + list(self.scope.get("markets") or [])
        for k in keys:
            for r in self.regimes.get(k, []):
                if r not in out:
                    out.append(r)
        for s in self.scope.get("sectors") or []:
            for r in self.sector_regimes.get(s, []):
                if r not in out:
                    out.append(r)
        return out

    # ---- lookups --------------------------------------------------------

    def activity(self, name: str | None) -> tuple[dict, str]:
        """Resolve an activity to its pattern. Returns (pattern, confidence).

        Unknown activities fall back to the stricter default rather than the
        looser one — under-flagging is the one failure an audit layer cannot
        afford, since a missed row is invisible.
        """
        if name and name in self.activities:
            return self.activities[name], "declared"
        return self.unknown_activity, "fallback"

    def provider(self, name: str | None, model: str | None = None) -> dict:
        """Resolve provider terms, falling back to the model's own prefix.

        The routing decision records a *lane* ("passthrough", "override") for an
        explicitly-addressed model, not the provider that actually served it. So
        "openrouter/deepseek/..." arrives as provider="passthrough" and would
        otherwise miss the registry entirely — losing the region, and with it the
        transfer flag, which is the one that matters most.
        """
        hit = self.providers.get(name or "")
        if hit:
            return hit
        prefix = (model or "").split("/", 1)[0].strip().lower()
        return self.providers.get(prefix, {})


def load_policy(path: str | Path) -> Policy:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Policy(raw)


# --- flag evaluation ------------------------------------------------------


def _flag(policy: Policy, rule_id: str, detail: str) -> Flag | None:
    rule = policy.flag_rules.get(rule_id)
    if not rule:
        return None
    return Flag(
        id=rule_id,
        severity=rule.get("severity", "high"),
        regime=rule.get("regime", ""),
        title=rule.get("title", rule_id),
        detail=detail,
    )


def evaluate_flags(
    policy: Policy,
    *,
    pattern: dict,
    data_class: str,
    provider_name: str | None,
    terms: dict,
    has_assessment: bool,
    has_human_review: bool,
) -> list[Flag]:
    """Decide what wants a human's attention. Never blocks anything."""
    flags: list[Flag] = []
    region = terms.get("region")
    hosting = terms.get("hosting")
    left_home = bool(region) and region not in policy.home_regions
    personal = data_class in ("personal", "sensitive")

    if personal and left_home and not terms.get("dpa"):
        flags.append(
            _flag(
                policy,
                "third-country-transfer",
                f"{data_class} data reached {provider_name} in {region} with no DPA "
                f"or transfer mechanism on record. The data has already gone.",
            )
        )
    if data_class == "secret" and hosting == "api":
        flags.append(
            _flag(
                policy,
                "secret-to-external",
                f"Secret-classified content was sent to {provider_name}, an external API.",
            )
        )
    if pattern.get("ai_act") == "prohibited":
        flags.append(
            _flag(
                policy,
                "prohibited-practice",
                "This activity is listed as a prohibited practice, not merely high-risk.",
            )
        )
    if pattern.get("ai_act") == "annex-iii" and not has_assessment:
        flags.append(
            _flag(
                policy,
                "high-risk-no-assessment",
                "Listed high-risk use with no impact assessment or approved system "
                "linked to this decision.",
            )
        )
    if pattern.get("effect") == "significant" and not has_human_review:
        flags.append(
            _flag(
                policy,
                "no-human-review",
                "A consequential decision was returned to the caller with no reviewer, "
                "notice or contest path recorded.",
            )
        )
    return [f for f in flags if f is not None]


def _resolved_provider(policy: Policy, name: str | None, model: str | None) -> str | None:
    """Name of the registry entry the terms came from — the lane if it matched,
    otherwise the model's prefix. Recorded so the row names a real destination."""
    if policy.providers.get(name or ""):
        return name
    prefix = (model or "").split("/", 1)[0].strip().lower()
    return prefix if policy.providers.get(prefix) else None


# --- trace construction ---------------------------------------------------


def build_trace(
    policy: Policy,
    *,
    activity: str | None = None,
    sensitivity: str = "none",
    provider_name: str | None = None,
    model: str | None = None,
    has_assessment: bool = False,
    has_human_review: bool = False,
) -> Trace:
    """Build the compliance trace for one call.

    ``activity`` is what the orchestrator declared (the highest-quality signal —
    a tool scope of ``mail:read`` on a business mailbox says band O no matter how
    innocent the prompt reads). ``sensitivity`` comes from the existing regex
    classifier. Everything else is looked up.
    """
    pattern, confidence = policy.activity(activity)
    data_class = _SENSITIVITY_TO_CLASS.get(sensitivity, "personal")
    terms = policy.provider(provider_name, model)
    region = terms.get("region")

    # The label is the strictest of the activity's reading and what the data
    # itself turned out to be — a "public" activity carrying secrets is not public.
    label = pattern.get("label", "O")
    if data_class == "secret":
        label = strictest(label, "O")
    elif data_class == "personal":
        label = strictest(label, "Y")

    resolved = _resolved_provider(policy, provider_name, model) or provider_name
    flags = evaluate_flags(
        policy,
        pattern=pattern,
        data_class=data_class,
        provider_name=resolved,
        terms=terms,
        has_assessment=has_assessment,
        has_human_review=has_human_review,
    )

    evaluated = policy.evaluated_against()
    # Only regimes inside the declared scope are reported as touched — that is
    # what stops every call blinking at every regime. The trace itself is built
    # regardless, so widening scope later re-illuminates this row.
    touched: list[str] = []
    if data_class in ("personal", "sensitive"):
        touched = [r for r in evaluated if "GDPR" in r or "FADP" in r]
    if pattern.get("ai_act"):
        touched += [r for r in evaluated if "AI-Act" in r and r not in touched]

    return Trace(
        activity=activity or "unknown",
        label=label,
        confidence=confidence,
        effect=pattern.get("effect", "draft"),
        data_class=data_class,
        evaluated_against=evaluated,
        regimes_touched=touched,
        destination={
            "provider": resolved,
            "region": region,
            "hosting": terms.get("hosting"),
            "third_country_transfer": bool(region) and region not in policy.home_regions,
        },
        provider_terms={
            "dpa": terms.get("dpa"),
            "trains": terms.get("trains"),
            "retention": terms.get("retention"),
        },
        flags=flags,
    )
