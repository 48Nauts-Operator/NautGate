"""Day 5a — 14-dimension complexity scorer (Tech Paper §2.2 + §4.3).

Pure function: ``score(payload) → ScoreVector`` (per-dimension floats 0..1)
plus a weighted aggregate. ``to_tier(vector)`` maps the aggregate to
``fast | balanced | deep | expert``. ``to_provider_model(tier, table)``
resolves a tier to a (provider, model) pair from the routing config.

The dimensions are heuristic — fast, deterministic, no LLM. They're meant to
do a competent first-pass triage; the brain layer (Week 2+) refines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 14 dimensions, each emits a 0..1 float.
DIMENSIONS: tuple[str, ...] = (
    "token_count",  # rough char/4 → token estimate, normalized
    "code_blocks",  # number of ```...``` fences
    "code_inline",  # number of `...` spans
    "math_markers",  # LaTeX-ish $...$ or \\(...\\)
    "reasoning_markers",  # "explain", "why", "step by step", "prove", "derive"
    "constraint_count",  # "must", "shall", "should", "do not", "never"
    "tool_calls",  # presence of OpenAI 'tools' / 'functions' field
    "system_complexity",  # length of system message text
    "image_presence",  # image content blocks
    "multi_turn",  # conversation depth
    "output_format_strict",  # explicit "respond with JSON", schema, format=json
    "domain_legal",  # legal jargon
    "domain_medical",  # medical jargon
    "language_non_english",  # heuristic for non-ASCII alphabet
)

# Default weights — uniform. Tweak later as we learn from production.
_DEFAULT_WEIGHTS: dict[str, float] = dict.fromkeys(DIMENSIONS, 1.0 / len(DIMENSIONS))

_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_RE_CODE_INLINE = re.compile(r"`[^`\n]+`")
_RE_MATH = re.compile(r"\\\(.*?\\\)|\\\[.*?\\\]|\$[^\$\n]+\$")
_RE_REASONING = re.compile(
    r"\b(?:explain|why|how does|step[- ]by[- ]step|prove|derive|reason about|justify|because)\b",
    re.IGNORECASE,
)
_RE_CONSTRAINTS = re.compile(
    r"\b(?:must|shall|should|do not|never|always|required|ensure|important)\b",
    re.IGNORECASE,
)
_RE_FORMAT = re.compile(
    r"\b(?:respond (?:with|in) json|output (?:as )?json|return (?:a )?json|json schema|"
    r"in yaml|markdown table|csv format|format[:=]\s*json)\b",
    re.IGNORECASE,
)
_RE_LEGAL = re.compile(
    r"\b(?:plaintiff|defendant|tort|liability|contract|covenant|jurisdiction|"
    r"statute|warranty|indemnif\w+|estoppel|GDPR|HIPAA|ADA|injunction|breach)\b",
    re.IGNORECASE,
)
_RE_MEDICAL = re.compile(
    r"\b(?:diagnos\w+|prescrib\w+|symptom|patient|dosage|mg/kg|mmHg|chronic|"
    r"acute|hypertension|diabet\w+|anesthe\w+|carcinoma)\b",
    re.IGNORECASE,
)
# Programming-domain prose markers — caught even without fenced code.
# Triggers a `deep`-tier floor so iOS / Android / framework questions don't
# land on chitchat-tier models (e.g. claude-haiku for SwiftUI = wrong fit).
_RE_PROGRAMMING_DOMAIN = re.compile(
    r"\b(?:"
    # Mobile / native
    r"ios|android|swift\w*|swiftui|uikit|xcode|kotlin|jetpack compose|objective-?c|"
    # Web frameworks / langs
    r"typescript|javascript|react(?:\.js)?|next\.js|vue|angular|svelte|nuxt|"
    r"python|django|fastapi|flask|node\.?js|deno|bun|"
    # Backend / systems
    r"rust|golang|\bgo lang\b|c\+\+|java(?!script)|scala|elixir|"
    # Data / infra
    r"postgres\w*|mysql|mongodb|redis|kafka|kubernetes|k8s|docker|terraform|"
    # Concepts that strongly imply real engineering work
    r"refactor|implement|debug|architecture|microservice|api endpoint|"
    r"authentication|authorization|migration|deployment|ci/cd|unit test"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class ScoreVector:
    dimensions: dict[str, float]
    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    # Auxiliary signals — used by tier-floor rules but not in the aggregate.
    aux: dict[str, float] = field(default_factory=dict)

    @property
    def aggregate(self) -> float:
        return sum(self.dimensions.get(k, 0.0) * w for k, w in self.weights.items())

    def signal(self, name: str) -> float:
        """Lookup a signal by name, checking dims first then aux."""
        if name in self.dimensions:
            return self.dimensions[name]
        return self.aux.get(name, 0.0)


def _saturate(x: float, scale: float) -> float:
    """Saturating-linear: 0..scale → 0..1, capped at 1."""
    if scale <= 0:
        return 0.0
    return min(1.0, x / scale)


def _all_user_text(messages: list[dict] | None) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    t = blk.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
    return "\n".join(parts)


def _system_text(messages: list[dict] | None) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def _has_image(messages: list[dict] | None) -> bool:
    if not messages:
        return False
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") in ("image", "image_url"):
                    return True
    return False


def _non_ascii_ratio(text: str) -> float:
    if not text:
        return 0.0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return non_ascii / len(text)


def score(payload: dict) -> ScoreVector:
    """Compute the 14-dimension complexity score for an OpenAI Chat-shaped payload."""
    messages = payload.get("messages") or []
    user_text = _all_user_text(messages)
    sys_text = _system_text(messages)
    combined = (user_text + "\n" + sys_text).strip()

    dims: dict[str, float] = {
        # ~4 chars/token. Saturate at 4000 tokens (~16k chars) → 1.0.
        "token_count": _saturate(len(combined), 16_000),
        # Each fence ≈ a code request; saturate at 5.
        "code_blocks": _saturate(len(_RE_CODE_BLOCK.findall(combined)), 5),
        "code_inline": _saturate(len(_RE_CODE_INLINE.findall(combined)), 20),
        "math_markers": _saturate(len(_RE_MATH.findall(combined)), 10),
        "reasoning_markers": _saturate(len(_RE_REASONING.findall(combined)), 5),
        "constraint_count": _saturate(len(_RE_CONSTRAINTS.findall(combined)), 8),
        "tool_calls": 1.0 if (payload.get("tools") or payload.get("functions")) else 0.0,
        "system_complexity": _saturate(len(sys_text), 4_000),
        "image_presence": 1.0 if _has_image(messages) else 0.0,
        "multi_turn": _saturate(len(messages), 12),
        "output_format_strict": 1.0
        if (_RE_FORMAT.search(combined) or payload.get("response_format"))
        else 0.0,
        "domain_legal": _saturate(len(_RE_LEGAL.findall(combined)), 4),
        "domain_medical": _saturate(len(_RE_MEDICAL.findall(combined)), 4),
        # 0..0.5 fraction maps to 0..1; anything beyond half non-ASCII is "heavily non-English".
        "language_non_english": _saturate(_non_ascii_ratio(combined), 0.5),
    }
    aux: dict[str, float] = {
        "domain_programming": _saturate(len(_RE_PROGRAMMING_DOMAIN.findall(combined)), 3),
    }
    return ScoreVector(dimensions=dims, aux=aux)


# --- Tier mapping ----------------------------------------------------------

# Aggregate thresholds, ascending. The intervals are:
#   [0, fast)        → fast
#   [fast, balanced) → balanced
#   [balanced, deep) → deep
#   [deep, ∞)        → expert
# Tuned so a complex code-reasoning request with tools + format spec lands in
# `deep`, leaving `expert` for the truly heavy stuff (multi-domain, long
# context, image + reasoning).
_TIER_THRESHOLDS = {
    "fast": 0.15,
    "balanced": 0.30,
    "deep": 0.50,
}


def to_tier(v: ScoreVector) -> str:
    a = v.aggregate
    base = (
        "fast"
        if a < _TIER_THRESHOLDS["fast"]
        else "balanced"
        if a < _TIER_THRESHOLDS["balanced"]
        else "deep"
        if a < _TIER_THRESHOLDS["deep"]
        else "expert"
    )

    # Specialty floors — averaging dilutes important signals. If the prompt has
    # *any* code or many tools, chitchat-tier models will produce wrong answers
    # regardless of how short the rest of the prompt is.
    code = v.dimensions.get("code_blocks", 0)
    code_inline = v.dimensions.get("code_inline", 0)
    tools = v.dimensions.get("tool_calls", 0)
    domain_legal = v.dimensions.get("domain_legal", 0)
    domain_medical = v.dimensions.get("domain_medical", 0)
    domain_programming = v.aux.get("domain_programming", 0)

    # Heavy code (3+ fenced blocks ≈ deep architectural work) → expert.
    # NOTE: tools+multi-turn alone is the *normal* state for any agentic CLI
    # (Pi, Claude Code, Aider) — it's not "expert" work. Don't promote on it.
    if code >= 0.40:
        return _max_tier(base, "expert")
    # Any code, rich inline code, any tools, sensitive domain, or programming
    # prose (e.g. "how do I add SwiftUI navigation?") → deep min.
    if (
        code > 0
        or code_inline >= 0.40
        or tools >= 1.0
        or domain_legal > 0
        or domain_medical > 0
        or domain_programming > 0  # any programming-domain marker
    ):
        return _max_tier(base, "deep")
    return base


_TIER_RANK = {"fast": 0, "balanced": 1, "deep": 2, "expert": 3}


def _max_tier(a: str, b: str) -> str:
    return a if _TIER_RANK.get(a, 0) >= _TIER_RANK.get(b, 0) else b


# --- Provider/model resolution from config ---------------------------------


@dataclass(frozen=True)
class ResolvedRoute:
    provider: str
    model: str
    fallback: tuple[str, str] | None = None


def load_routing_table(path: str | Path) -> dict[str, dict]:
    """Load and validate config/routing.yaml. Returns the `tiers` mapping."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tiers = raw.get("tiers") or {}
    if not isinstance(tiers, dict):
        raise ValueError("routing.yaml: top-level `tiers` must be a mapping")
    for name, body in tiers.items():
        if not isinstance(body, dict) or "primary" not in body:
            raise ValueError(f"routing.yaml: tier {name!r} missing required `primary`")
        for slot in ("primary", "fallback"):
            if slot not in body:
                continue
            entry = body[slot]
            if not isinstance(entry, dict) or not entry.get("provider") or not entry.get("model"):
                raise ValueError(f"routing.yaml: tier {name!r}.{slot} needs provider+model")
    return tiers


def resolve(tier: str, table: dict[str, dict]) -> ResolvedRoute:
    body = table.get(tier)
    if body is None:
        # Fall back to "balanced" if the tier is unknown.
        body = table.get("balanced")
        if body is None:
            raise KeyError(f"tier {tier!r} not in routing table and no balanced fallback")
    primary = body["primary"]
    fb_entry = body.get("fallback")
    fb: tuple[str, str] | None = None
    if isinstance(fb_entry, dict):
        fb = (fb_entry["provider"], fb_entry["model"])
    return ResolvedRoute(provider=primary["provider"], model=primary["model"], fallback=fb)


def to_provider_model(tier: str, table: dict[str, dict]) -> tuple[str, str]:
    r = resolve(tier, table)
    return (r.provider, r.model)


def resolve_healthy(
    tier: str,
    table: dict[str, dict],
    is_unhealthy_fn,
    *,
    banned_models: list[str] | tuple[str, ...] = (),
) -> ResolvedRoute:
    """Like ``resolve`` but skips the primary if it's unhealthy OR banned.

    Falls through to the fallback for the same reasons. If both primary and
    fallback are unavailable, returns the primary anyway (don't strand the request).
    """
    banned = set(banned_models or ())
    primary = resolve(tier, table)
    primary_blocked = primary.model in banned or is_unhealthy_fn(primary.provider, primary.model)
    if not primary_blocked:
        return primary
    if primary.fallback is None:
        return primary
    fb_provider, fb_model = primary.fallback
    if fb_model in banned:
        return primary
    return ResolvedRoute(provider=fb_provider, model=fb_model, fallback=None)


# --- Glue ------------------------------------------------------------------


def score_and_route(
    payload: dict,
    table: dict[str, dict] | None,
) -> tuple[ScoreVector, str, ResolvedRoute | None]:
    """Convenience: score → tier → resolve. Used by Day 5b's `model: "auto"` path."""
    vector = score(payload)
    tier = to_tier(vector)
    route = None if table is None else resolve(tier, table)
    return vector, tier, route
