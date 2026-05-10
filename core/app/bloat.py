"""Bloat detection — finds requests that ship more data than the task needs.

Pure function module. Given the payload anatomy, tier, tools/calls history, and
the actual cost of the request, returns a list of findings + an estimated
"wasted" cost (the bytes that didn't need to ship, billed at the input rate).

Four signals:

  excessive_context       user_pct < 1%        — your typed bytes are a
                                                 vanishing fraction of payload
  history_dominance       history_pct > 80%    — old turns drag every request
  unused_capabilities     tools shipped but    — schemas you're paying to
                          recent tool-call         re-ship without using
                          ratio < 0.20
  oversized_for_tier      total > 2× envelope  — over-budget for what the
                          for the tier             scorer thinks this needs

Severity ladder: info < warn < crit. Each finding carries a score_penalty in
[0, 1] which the scorecard subtracts (with time decay) from the model's score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Bytes envelopes by tier — the rough size we'd expect a request to ship at
# each tier. Above 2× this is "oversized for the task." Tuned conservatively;
# `deep` and `expert` get more room because long context is often the point.
TIER_ENVELOPE_BYTES: dict[str, int] = {
    "fast": 5_000,
    "balanced": 30_000,
    "deep": 200_000,
    "expert": 500_000,
}

# Score penalty by severity. Penalties stack across findings on one request.
PENALTY_BY_SEVERITY: dict[str, float] = {
    "info": 0.005,
    "warn": 0.020,
    "crit": 0.060,
}


@dataclass(frozen=True)
class BloatFinding:
    finding_type: str
    severity: str  # "info" | "warn" | "crit"
    score_penalty: float  # in [0, 1]
    detail: str  # human-readable explanation, e.g. "user_pct=0.08% (threshold 1%)"
    estimated_waste_bytes: int  # bytes attributable to this finding

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.finding_type,
            "severity": self.severity,
            "penalty": self.score_penalty,
            "detail": self.detail,
            "waste_bytes": self.estimated_waste_bytes,
        }


def _severity_from_ratio(observed: float, threshold: float, *, lower_is_worse: bool = True) -> str:
    """Map how-far-past-threshold to severity. ``lower_is_worse`` flips the
    direction (used for user_pct where 0% is worst).
    """
    if lower_is_worse:
        # observed below threshold is bad. crit at <50% of threshold, warn at <100%.
        if observed >= threshold:
            return "info"
        if observed >= threshold * 0.5:
            return "warn"
        return "crit"
    else:
        # observed above threshold is bad. crit at >2× threshold, warn at >threshold.
        if observed <= threshold:
            return "info"
        if observed <= threshold * 2:
            return "warn"
        return "crit"


def compute_bloat(
    payload_anatomy: dict | None,
    *,
    classified_tier: str,
    tools_count: int | None,
    tool_calls_made_count: int = 0,
    input_price_per_million: float | None = None,
) -> tuple[list[BloatFinding], float]:
    """Run the four bloat detectors. Returns (findings, estimated_waste_usd).

    ``payload_anatomy`` is the dict produced by ``queries._payload_anatomy`` —
    has keys ``system``, ``tools``, ``history``, ``user``, ``totals``. When
    None or missing totals (body suppressed by capture policy), no findings.
    """
    if not payload_anatomy or not payload_anatomy.get("totals"):
        return [], 0.0

    totals = payload_anatomy["totals"]
    total_bytes = totals.get("bytes", 0)
    if total_bytes <= 0:
        return [], 0.0

    user_bytes = payload_anatomy.get("user", {}).get("bytes", 0)
    history_bytes = payload_anatomy.get("history", {}).get("bytes", 0)
    tools_bytes = payload_anatomy.get("tools", {}).get("bytes", 0)
    user_pct = user_bytes / total_bytes
    history_pct = history_bytes / total_bytes

    findings: list[BloatFinding] = []
    waste_bytes_total = 0

    # 1) excessive_context — your typed bytes are <1% of payload.
    USER_PCT_THRESHOLD = 0.01
    if user_pct < USER_PCT_THRESHOLD and total_bytes > 2_000:
        sev = _severity_from_ratio(user_pct, USER_PCT_THRESHOLD, lower_is_worse=True)
        # Waste = everything except what the user actually contributed.
        # This is conservative — some context is needed; we don't know how much.
        # Attribute 30% of non-user bytes as "probably wasted" for accounting.
        waste_b = int(0.30 * (total_bytes - user_bytes))
        findings.append(
            BloatFinding(
                finding_type="excessive_context",
                severity=sev,
                score_penalty=PENALTY_BY_SEVERITY[sev],
                detail=f"user_pct={user_pct * 100:.2f}% (threshold {USER_PCT_THRESHOLD * 100:.0f}%) of {total_bytes:,}B",
                estimated_waste_bytes=waste_b,
            )
        )
        waste_bytes_total += waste_b

    # 2) history_dominance — history is most of the payload. Old turns get
    #    re-shipped on every request, billed every time.
    HISTORY_PCT_THRESHOLD = 0.80
    if history_pct > HISTORY_PCT_THRESHOLD and history_bytes > 10_000:
        sev = _severity_from_ratio(history_pct, HISTORY_PCT_THRESHOLD, lower_is_worse=False)
        # Waste: portion of history above the 80% threshold is "extra dragging."
        excess_pct = history_pct - HISTORY_PCT_THRESHOLD
        waste_b = int(excess_pct * total_bytes)
        findings.append(
            BloatFinding(
                finding_type="history_dominance",
                severity=sev,
                score_penalty=PENALTY_BY_SEVERITY[sev],
                detail=f"history_pct={history_pct * 100:.1f}% ({history_bytes:,}B of {total_bytes:,}B)",
                estimated_waste_bytes=waste_b,
            )
        )
        waste_bytes_total += waste_b

    # 3) unused_capabilities — many tools declared, few invoked. Each unused
    #    tool schema is paid-for-but-not-used data going to the provider.
    UNUSED_RATIO_THRESHOLD = 0.20
    if tools_count and tools_count >= 5 and tools_bytes > 1_000:
        used_ratio = (tool_calls_made_count / tools_count) if tools_count else 0.0
        if used_ratio < UNUSED_RATIO_THRESHOLD:
            sev = _severity_from_ratio(used_ratio, UNUSED_RATIO_THRESHOLD, lower_is_worse=True)
            unused_count = tools_count - tool_calls_made_count
            # Waste: proportion of tools not invoked × tools section bytes.
            waste_b = int((unused_count / tools_count) * tools_bytes)
            findings.append(
                BloatFinding(
                    finding_type="unused_capabilities",
                    severity=sev,
                    score_penalty=PENALTY_BY_SEVERITY[sev],
                    detail=f"{tool_calls_made_count} of {tools_count} tools invoked ({used_ratio * 100:.0f}%); {unused_count} schemas shipped unused",
                    estimated_waste_bytes=waste_b,
                )
            )
            waste_bytes_total += waste_b

    # 4) oversized_for_tier — request is way over the typical envelope for
    #    the classified tier. Could mean the scorer underclassified or the
    #    agent shipped too much.
    envelope = TIER_ENVELOPE_BYTES.get(classified_tier, 50_000)
    if total_bytes > 2 * envelope:
        ratio = total_bytes / envelope
        sev = _severity_from_ratio(ratio, 2.0, lower_is_worse=False)
        waste_b = total_bytes - envelope
        findings.append(
            BloatFinding(
                finding_type="oversized_for_tier",
                severity=sev,
                score_penalty=PENALTY_BY_SEVERITY[sev],
                detail=f"{total_bytes:,}B = {ratio:.1f}× envelope for tier '{classified_tier}' ({envelope:,}B)",
                estimated_waste_bytes=waste_b,
            )
        )
        # Don't double-count waste — oversized_for_tier overlaps with the
        # other findings. Take the max attribution rather than sum.
        waste_bytes_total = max(waste_bytes_total, waste_b)

    # Convert wasted bytes to wasted USD using a rough byte→token ratio (4:1).
    estimated_waste_usd = 0.0
    if input_price_per_million and waste_bytes_total > 0:
        wasted_tokens = waste_bytes_total / 4
        estimated_waste_usd = (wasted_tokens / 1_000_000) * input_price_per_million

    return findings, estimated_waste_usd


def aggregate_score_penalty(findings: list[BloatFinding]) -> float:
    """Sum penalties, capped at the per-request limit so one bad call can't
    nuke a model's reputation. Cap = 0.10 (model loses at most 0.10 per call).
    """
    return min(0.10, sum(f.score_penalty for f in findings))
