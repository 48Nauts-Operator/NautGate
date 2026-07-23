"""Per-(provider, model, tier) scorecard — the brain's working memory.

Every request that lands at a model produces a score update:
  - clean request (no findings) → small reward toward 0.50 ceiling-baseline,
    or full restoration toward 1.0 if score has drifted high
  - request with findings → subtract penalty, with diminishing returns
    (capped per-call so one bad request can't tank a model)

Time decay: rather than a separate background job, we apply decay lazily on
each update — the longer since ``last_updated``, the more the score drifts
back toward the neutral 0.50. Half-life of 7 days.

Routing reads scorecard via ``get_score(provider, model, tier)`` and skips
models whose score is below the configured threshold (default 0.30).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import asyncpg

from app.bloat import BloatFinding, aggregate_score_penalty

# Half-life of penalties: a 7-day-old incident counts half as much.
DECAY_HALF_LIFE_SECONDS = 7 * 24 * 3600

# Reward magnitude per clean request — small, so a well-behaving model
# accumulates trust over many calls rather than rebounding instantly.
CLEAN_REQUEST_REWARD = 0.005

# Floor / ceiling for stored score.
SCORE_FLOOR = 0.0
SCORE_CEILING = 1.0
NEUTRAL_SCORE = 0.500

# Below this score, routing demotes the model.
DEMOTION_THRESHOLD_DEFAULT = 0.30


@dataclass(frozen=True)
class ScorecardEntry:
    provider: str
    model: str
    tier: str
    score: float
    sample_size: int
    total_waste_usd: float


def _decay_toward_neutral(current: float, seconds_elapsed: float) -> float:
    """Pull current score toward NEUTRAL_SCORE based on elapsed time.

    Uses exponential decay with DECAY_HALF_LIFE_SECONDS — a score sitting at
    0.10 will be at 0.30 after 7 days untouched, 0.40 after 14 days, etc.
    """
    if seconds_elapsed <= 0:
        return current
    decay_factor = math.exp(-math.log(2) * seconds_elapsed / DECAY_HALF_LIFE_SECONDS)
    return NEUTRAL_SCORE + (current - NEUTRAL_SCORE) * decay_factor


def _clamp(x: float) -> float:
    return max(SCORE_FLOOR, min(SCORE_CEILING, x))


async def apply_findings(
    pool: asyncpg.Pool,
    *,
    decision_id,
    provider: str,
    model: str,
    tier: str,
    findings: list[BloatFinding],
    estimated_waste_usd: float,
) -> ScorecardEntry:
    """Update scorecard for (provider, model, tier) based on this request.

    Also writes one ``model_incidents`` row per finding, linking back to the
    decision_id for audit-trail click-through.
    """
    penalty = aggregate_score_penalty(findings)
    # Clean requests get a small reward; bad requests get the penalty.
    delta = -penalty if findings else CLEAN_REQUEST_REWARD

    async with pool.acquire() as conn, conn.transaction():
        # 1) Upsert + apply time-decay + delta.
        row = await conn.fetchrow(
            """
            SELECT score, sample_size, total_waste_usd, last_updated
              FROM nautgate.model_scorecard
             WHERE provider = $1 AND model = $2 AND tier = $3
             FOR UPDATE
            """,
            provider,
            model,
            tier,
        )

        if row is None:
            # First sighting of this (provider, model, tier).
            new_score = _clamp(NEUTRAL_SCORE + delta)
            new_sample = 1
            new_waste = float(estimated_waste_usd or 0)
            await conn.execute(
                """
                INSERT INTO nautgate.model_scorecard
                    (provider, model, tier, score, sample_size, total_waste_usd, last_updated)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                """,
                provider,
                model,
                tier,
                new_score,
                new_sample,
                new_waste,
            )
        else:
            elapsed = (
                await conn.fetchval("SELECT EXTRACT(EPOCH FROM (now() - $1))", row["last_updated"])
            ) or 0.0
            decayed = _decay_toward_neutral(float(row["score"]), float(elapsed))
            new_score = _clamp(decayed + delta)
            new_sample = row["sample_size"] + 1
            new_waste = float(row["total_waste_usd"]) + float(estimated_waste_usd or 0)
            await conn.execute(
                """
                UPDATE nautgate.model_scorecard
                   SET score = $4,
                       sample_size = $5,
                       total_waste_usd = $6,
                       last_updated = now()
                 WHERE provider = $1 AND model = $2 AND tier = $3
                """,
                provider,
                model,
                tier,
                new_score,
                new_sample,
                new_waste,
            )

        # 2) Write one incident row per finding (audit trail).
        for f in findings:
            await conn.execute(
                """
                INSERT INTO nautgate.model_incidents
                    (provider, model, tier, decision_id, finding_type,
                     severity, score_penalty, estimated_waste_usd)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                provider,
                model,
                tier,
                decision_id,
                f.finding_type,
                f.severity,
                f.score_penalty,
                # Per-finding cost share, proportional to its byte attribution.
                _per_finding_waste_usd(f, findings, estimated_waste_usd),
            )

    return ScorecardEntry(
        provider=provider,
        model=model,
        tier=tier,
        score=new_score,
        sample_size=new_sample,
        total_waste_usd=new_waste,
    )


def _per_finding_waste_usd(
    f: BloatFinding, all_findings: list[BloatFinding], total_usd: float
) -> float:
    """Split the request's wasted USD across findings by waste_bytes share."""
    total_b = sum(x.estimated_waste_bytes for x in all_findings) or 1
    return total_usd * (f.estimated_waste_bytes / total_b)


async def get_score(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    tier: str,
) -> float:
    """Return the current (decay-applied) score for (provider, model, tier).

    Returns NEUTRAL_SCORE (0.50) when no row exists — every model gets the
    benefit of the doubt before its first sample lands.
    """
    row = await pool.fetchrow(
        """
        SELECT score, last_updated
          FROM nautgate.model_scorecard
         WHERE provider = $1 AND model = $2 AND tier = $3
        """,
        provider,
        model,
        tier,
    )
    if row is None:
        return NEUTRAL_SCORE
    elapsed = (
        await pool.fetchval("SELECT EXTRACT(EPOCH FROM (now() - $1))", row["last_updated"])
    ) or 0.0
    return _decay_toward_neutral(float(row["score"]), float(elapsed))


async def is_demoted(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    tier: str,
    threshold: float = DEMOTION_THRESHOLD_DEFAULT,
) -> tuple[bool, float]:
    """Returns (is_demoted, current_score)."""
    s = await get_score(pool, provider=provider, model=model, tier=tier)
    return s < threshold, s


async def process_brain(
    pool: asyncpg.Pool,
    pricing,
    *,
    decision_id,
    actual_provider: str | None = None,
    actual_model: str | None = None,
) -> None:
    """Post-outcome brain step: compute bloat findings + update scorecard.

    Reads the just-completed decision (with payload_anatomy + tool_calls_made),
    computes bloat findings using ``bloat.compute_bloat``, writes the findings
    to ``route_decisions.bloat_findings``, and updates the scorecard for
    whichever (provider, model) actually served the request.

    Falls back to ``decision_provider/decision_model`` when actual_* are null
    (passthrough requests, or providers that don't echo the picked model).
    """
    from app.bloat import compute_bloat
    from app.db import queries

    # Fetch the decision (this also computes payload_anatomy from the captured
    # bodies — see queries.get_decision_detail).
    row = await pool.fetchrow(
        """
        SELECT d.id::text                       AS decision_id,
               d.agent_id                       AS agent_id,
               d.classified_tier                AS classified_tier,
               d.decision_provider              AS decision_provider,
               d.decision_model                 AS decision_model,
               d.tools_count                    AS tools_count,
               d.prompt_body                    AS prompt_body,
               d.tools_body                     AS tools_body,
               o.tool_calls_made                AS tool_calls_made,
               o.actual_model                   AS actual_model,
               o.actual_provider                AS actual_provider
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE d.id::text = $1
        """,
        str(decision_id),
    )
    if row is None:
        return

    # Build payload_anatomy fresh (queries._payload_anatomy is the source of truth).
    from app.audit_meta import _content_text, _estimate_tokens

    anatomy = queries._payload_anatomy(
        row["prompt_body"], row["tools_body"], _content_text, _estimate_tokens
    )

    # Score against the *routed* pair, not the underlying inference provider.
    # Routing demotion looks up by (decision_provider, decision_model), so the
    # scorecard must use the same key. The actual_* fields tell us which deep
    # inference provider OpenRouter picked under the hood (e.g. Novita) — useful
    # context but not the right key for our demotion logic.
    eff_provider = row["decision_provider"]
    eff_model = row["decision_model"]
    tier = row["classified_tier"] or "balanced"

    # Tool-call count from the parsed tool_calls_made jsonb (list of calls).
    tool_calls_made = row["tool_calls_made"]
    if isinstance(tool_calls_made, str):
        try:
            import json

            tool_calls_made = json.loads(tool_calls_made)
        except (ValueError, TypeError):
            tool_calls_made = None
    tool_calls_made_count = len(tool_calls_made) if isinstance(tool_calls_made, list) else 0

    # Pricing lookup for waste-USD attribution.
    price = pricing.lookup(eff_provider, eff_model) if pricing is not None else None
    input_price = price.input if price else None

    findings, waste_usd = compute_bloat(
        anatomy,
        classified_tier=tier,
        tools_count=row["tools_count"],
        tool_calls_made_count=tool_calls_made_count,
        input_price_per_million=input_price,
    )

    # Persist bloat data on the decision row (UPDATE — fire-and-forget).
    import json

    findings_json = json.dumps([f.to_dict() for f in findings]) if findings else None
    bloat_score = aggregate_score_penalty(findings)  # 0 .. 0.10 cap
    await pool.execute(
        """
        UPDATE nautgate.route_decisions
           SET bloat_findings      = $2::jsonb,
               bloat_score         = $3,
               estimated_waste_usd = $4
         WHERE id::text = $1
        """,
        str(decision_id),
        findings_json,
        bloat_score,
        waste_usd if waste_usd > 0 else None,
    )

    # Apply to scorecard + write per-finding incidents.
    if eff_provider and eff_model:
        await apply_findings(
            pool,
            decision_id=decision_id,
            provider=eff_provider,
            model=eff_model,
            tier=tier,
            findings=findings,
            estimated_waste_usd=waste_usd,
        )


async def get_scorecard_with_incidents(
    pool: asyncpg.Pool,
    *,
    incidents_per_row: int = 5,
) -> list[dict]:
    """Return all scorecard rows + their N most-recent incidents (for the UI tab)."""
    rows = await pool.fetch(
        """
        SELECT provider, model, tier, score, sample_size, total_waste_usd, last_updated
          FROM nautgate.model_scorecard
         ORDER BY tier, score ASC
        """
    )
    out: list[dict] = []
    for r in rows:
        elapsed = (
            await pool.fetchval("SELECT EXTRACT(EPOCH FROM (now() - $1))", r["last_updated"])
        ) or 0.0
        live_score = _decay_toward_neutral(float(r["score"]), float(elapsed))
        incidents = await pool.fetch(
            """
            SELECT id::text, decision_id::text, finding_type, severity,
                   score_penalty, estimated_waste_usd, ts
              FROM nautgate.model_incidents
             WHERE provider = $1 AND model = $2 AND tier = $3
             ORDER BY ts DESC
             LIMIT $4
            """,
            r["provider"],
            r["model"],
            r["tier"],
            incidents_per_row,
        )
        out.append(
            {
                "provider": r["provider"],
                "model": r["model"],
                "tier": r["tier"],
                "score_stored": float(r["score"]),
                "score": live_score,  # decay-applied "as of now"
                "sample_size": r["sample_size"],
                "total_waste_usd": float(r["total_waste_usd"]),
                "last_updated": r["last_updated"].isoformat() if r["last_updated"] else None,
                "is_demoted": live_score < DEMOTION_THRESHOLD_DEFAULT,
                "recent_incidents": [
                    {
                        "id": inc["id"],
                        "decision_id": inc["decision_id"],
                        "finding_type": inc["finding_type"],
                        "severity": inc["severity"],
                        "score_penalty": float(inc["score_penalty"]),
                        "estimated_waste_usd": float(inc["estimated_waste_usd"] or 0),
                        "ts": inc["ts"].isoformat() if inc["ts"] else None,
                    }
                    for inc in incidents
                ],
            }
        )
    return out
