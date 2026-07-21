"""Insights — next-level analytics over the audit log.

Six read-only panels, each one SQL aggregate + a pure computation function
(the math is unit-tested without a DB):

  simulator     — counterfactual cost replay under alternative routing policies
  substitution  — judged quality impact of silent model substitutions
  spc           — EWMA control charts per model (statistical process control)
  efficiency    — composite 0–100 Gateway Efficiency Index per agent
  dataflow      — agent → data category → provider flow (privacy map)
  overthinking  — reasoning-token share vs judged task completion

Cost semantics: OAuth/subscription calls carry notional_cost_usd (the
metered-equivalent) instead of cost_usd — every sum here uses
COALESCE(cost_usd, notional_cost_usd, 0) so subscription traffic is priced
at what it WOULD have cost, which is the honest baseline for counterfactuals.
"""

from __future__ import annotations

import math
import re
from typing import Any

import asyncpg

from app.findings import CATEGORY

# ── Simulator ────────────────────────────────────────────────────────────────

# Canned counterfactual policies: route EVERYTHING to one target and reprice.
# ponytail: whole-traffic substitution, per-tier policies when someone asks.
POLICIES: dict[str, tuple[str, str]] = {
    "claude-haiku-4-5": ("anthropic", "claude-haiku-4-5"),
    "claude-sonnet-4-6": ("anthropic", "claude-sonnet-4-6"),
    "gpt-4o-mini (OpenRouter)": ("openrouter", "openrouter/openai/gpt-4o-mini"),
}


def simulate_costs(rows: list[dict], pricing, target: tuple[str, str]) -> dict:
    """Reprice each call's token usage at the target model. Pure."""
    provider, model = target
    actual = simulated = 0.0
    priced = unpriced = 0
    for r in rows:
        actual += float(r.get("actual_usd") or 0)
        c = pricing.compute_cost(
            provider, model,
            prompt_tokens=r.get("prompt_tokens"),
            completion_tokens=r.get("completion_tokens"),
            cache_read_tokens=r.get("cache_read_tokens"),
            cache_write_tokens=r.get("cache_write_tokens"),
        ) if pricing else None
        if c is None:
            unpriced += 1
        else:
            priced += 1
            simulated += c
    return {
        "actual_usd": round(actual, 4),
        "simulated_usd": round(simulated, 4),
        "savings_usd": round(actual - simulated, 4),
        "calls": len(rows),
        "priced_calls": priced,
        "unpriced_calls": unpriced,
    }


# ── Substitution impact ──────────────────────────────────────────────────────


def _welch(mean_a, var_a, n_a, mean_b, var_b, n_b) -> float | None:
    """Two-sided p-value, Welch's t with normal approximation. Pure."""
    if n_a < 2 or n_b < 2:
        return None
    se2 = var_a / n_a + var_b / n_b
    if se2 <= 0:
        return None
    t = (mean_a - mean_b) / math.sqrt(se2)
    # Normal approximation is fine at the n we care about (>=5 per side).
    return round(math.erfc(abs(t) / math.sqrt(2)), 4)


_SNAPSHOT_RE = re.compile(r"-20\d{6}$")


def _normalize_model(m: str) -> str:
    """Date-suffixed snapshots are the same model, not a substitution."""
    return _SNAPSHOT_RE.sub("", m or "")


def substitution_impact(rows: list[dict], min_n: int = 5) -> list[dict]:
    """Group judged calls by requested model; compare served-as-asked vs
    silently-substituted scores. rows: {asked, served, score}. Pure."""
    by_asked: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        asked = _normalize_model(r["asked"])
        served = _normalize_model(r["served"])
        score = r["score"]
        if score is None:
            continue
        slot = by_asked.setdefault(asked, {"match": [], "sub": {}})
        if served == asked:
            slot["match"].append(float(score))
        else:
            slot["sub"].setdefault(served, []).append(float(score))
    out = []
    for asked, slot in by_asked.items():
        match = slot["match"]
        if len(match) < min_n:
            continue
        m_mean = sum(match) / len(match)
        m_var = sum((x - m_mean) ** 2 for x in match) / max(1, len(match) - 1)
        for served, scores in slot["sub"].items():
            if len(scores) < min_n:
                continue
            s_mean = sum(scores) / len(scores)
            s_var = sum((x - s_mean) ** 2 for x in scores) / max(1, len(scores) - 1)
            out.append({
                "asked": asked,
                "served": served,
                "n_substituted": len(scores),
                "n_as_asked": len(match),
                "mean_substituted": round(s_mean, 2),
                "mean_as_asked": round(m_mean, 2),
                "delta": round(s_mean - m_mean, 2),
                "p_value": _welch(s_mean, s_var, len(scores), m_mean, m_var, len(match)),
            })
    out.sort(key=lambda x: x["delta"])
    return out


# ── SPC / EWMA control chart ────────────────────────────────────────────────


def ewma_chart(values: list[float], lam: float = 0.2, sigmas: float = 3.0) -> dict:
    """EWMA series + control limits from the series' own mean/sd. Pure.

    Control limits use the standard EWMA variance term
    sd * sqrt(lam/(2-lam) * (1-(1-lam)^{2i})), widening toward its asymptote.
    """
    n = len(values)
    if n == 0:
        return {"ewma": [], "ucl": [], "lcl": [], "violations": [], "mean": None, "sd": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / max(1, n - 1))
    ewma, ucl, lcl, violations = [], [], [], []
    z = mean
    for i, v in enumerate(values):
        z = lam * v + (1 - lam) * z
        ewma.append(round(z, 4))
        width = sigmas * sd * math.sqrt(lam / (2 - lam) * (1 - (1 - lam) ** (2 * (i + 1))))
        ucl.append(round(mean + width, 4))
        lcl.append(round(mean - width, 4))
        if z > ucl[-1] or z < lcl[-1]:
            violations.append(i)
    return {"ewma": ewma, "ucl": ucl, "lcl": lcl, "violations": violations,
            "mean": round(mean, 4), "sd": round(sd, 4)}


SPC_METRICS = {
    "completion_tokens": "AVG(o.completion_tokens)",
    "reasoning_share": "AVG(CASE WHEN COALESCE(o.completion_tokens,0)+COALESCE(o.reasoning_tokens,0) > 0 "
                       "THEN COALESCE(o.reasoning_tokens,0)::float/(COALESCE(o.completion_tokens,0)+COALESCE(o.reasoning_tokens,0)) END)",
    "first_byte_ms": "AVG(o.first_byte_ms)",
    "empty_rate": "AVG(CASE WHEN o.was_empty THEN 1.0 ELSE 0.0 END)",
    "tool_calls": "AVG(CASE WHEN jsonb_typeof(o.tool_calls_made) = 'array' "
                  "THEN jsonb_array_length(o.tool_calls_made) ELSE 0 END)",
}


# ── Efficiency index ────────────────────────────────────────────────────────

# Component weights; renormalized over whichever components have data.
EFFICIENCY_WEIGHTS = {
    "quality": 0.30,     # avg judged task_completion / 5
    "relevance": 0.20,   # 1 - avg irrelevant_share/100
    "waste": 0.20,       # 1 - waste_usd / cost_usd
    "cache": 0.15,       # cache_read / (cache_read + fresh prompt)
    "bloat": 0.15,       # 1 - avg bloat penalty * 5 (penalty ~[0, 0.2])
}


def efficiency_score(c: dict) -> dict:
    """Composite 0–100 from available components. Pure.
    c keys (any may be None): quality(0-5), irrelevant_share(0-100),
    cost_usd, waste_usd, cache_read_tokens, fresh_prompt_tokens, avg_bloat.
    """
    comps: dict[str, float] = {}
    if c.get("quality") is not None:
        comps["quality"] = max(0.0, min(1.0, float(c["quality"]) / 5.0))
    if c.get("irrelevant_share") is not None:
        comps["relevance"] = max(0.0, min(1.0, 1.0 - float(c["irrelevant_share"]) / 100.0))
    cost = float(c.get("cost_usd") or 0)
    if cost > 0:
        comps["waste"] = max(0.0, min(1.0, 1.0 - float(c.get("waste_usd") or 0) / cost))
    reads = float(c.get("cache_read_tokens") or 0)
    fresh = float(c.get("fresh_prompt_tokens") or 0)
    if reads + fresh > 0:
        comps["cache"] = reads / (reads + fresh)
    if c.get("avg_bloat") is not None:
        comps["bloat"] = max(0.0, min(1.0, 1.0 - float(c["avg_bloat"]) * 5.0))
    if not comps:
        return {"score": None, "components": {}}
    total_w = sum(EFFICIENCY_WEIGHTS[k] for k in comps)
    score = sum(comps[k] * EFFICIENCY_WEIGHTS[k] for k in comps) / total_w
    return {
        "score": round(score * 100),
        "components": {k: round(v * 100) for k, v in comps.items()},
    }


# ── Queries (thin — all shaping happens in the pure functions above) ────────


async def q_simulator(pool: asyncpg.Pool, pricing, hours: int) -> dict:
    rows = [dict(r) for r in await pool.fetch(
        """
        SELECT COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0)::float AS actual_usd,
               o.prompt_tokens, o.completion_tokens,
               o.cache_read_tokens, o.cache_write_tokens
          FROM nautgate.route_decisions d
          JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(hours => $1)
           AND o.status_code BETWEEN 200 AND 299
        """, hours)]
    quality = {r["model"]: {"quality": float(r["quality"]), "n": r["n"]}
               for r in await pool.fetch(
        """
        SELECT COALESCE(o.actual_model, d.decision_model) AS model,
               AVG((q.rubric->>'task_completion')::numeric) AS quality,
               COUNT(*) AS n
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE q.rubric ? 'task_completion'
         GROUP BY 1
        """)}
    policies = []
    for name, target in POLICIES.items():
        sim = simulate_costs(rows, pricing, target)
        tq = quality.get(target[1]) or quality.get(f"{target[0]}/{target[1]}")
        sim["policy"] = name
        sim["target_quality"] = tq  # None when we've never judged that model
        policies.append(sim)
    # Traffic-weighted current quality across evaluated calls.
    tot_n = sum(v["n"] for v in quality.values())
    current_q = (sum(v["quality"] * v["n"] for v in quality.values()) / tot_n) if tot_n else None
    return {"hours": hours, "policies": policies,
            "current_quality": round(current_q, 2) if current_q is not None else None,
            "evaluated_calls": tot_n}


async def q_substitution(pool: asyncpg.Pool) -> dict:
    rows = [dict(r) for r in await pool.fetch(
        """
        SELECT d.decision_model AS asked,
               COALESCE(o.actual_model, d.decision_model) AS served,
               (q.rubric->>'task_completion')::float AS score
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE q.rubric ? 'task_completion'
        """)]
    return {"pairs": substitution_impact(rows), "judged_calls": len(rows)}


async def q_spc(pool: asyncpg.Pool, metric: str, hours: int, top_models: int = 4) -> dict:
    expr = SPC_METRICS.get(metric)
    if expr is None:
        raise ValueError(f"unknown metric {metric!r}")
    rows = await pool.fetch(
        f"""
        SELECT COALESCE(o.actual_model, d.decision_model) AS model,
               date_trunc('hour', o.ts) AS bucket,
               {expr}::float AS value,
               COUNT(*) AS n
          FROM nautgate.route_decisions d
          JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE o.ts > NOW() - make_interval(hours => $1)
           AND o.status_code BETWEEN 200 AND 299
         GROUP BY 1, 2
         ORDER BY 1, 2
        """, hours)
    by_model: dict[str, dict] = {}
    for r in rows:
        m = by_model.setdefault(r["model"], {"buckets": [], "values": [], "calls": 0})
        if r["value"] is not None:
            m["buckets"].append(int(r["bucket"].timestamp()))
            m["values"].append(float(r["value"]))
        m["calls"] += int(r["n"])
    top = sorted(by_model.items(), key=lambda kv: -kv[1]["calls"])[:top_models]
    models = []
    for name, m in top:
        if len(m["values"]) < 4:  # too few buckets for limits to mean anything
            continue
        chart = ewma_chart(m["values"])
        models.append({"model": name, "calls": m["calls"], "buckets": m["buckets"],
                       "values": [round(v, 4) for v in m["values"]], **chart})
    return {"metric": metric, "hours": hours, "models": models}


async def q_efficiency(pool: asyncpg.Pool, days: int) -> dict:
    rows = await pool.fetch(
        """
        SELECT d.agent_id,
               COUNT(*) AS calls,
               AVG(d.bloat_score)::float AS avg_bloat,
               SUM(COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0))::float AS cost_usd,
               SUM(COALESCE(d.estimated_waste_usd, 0))::float AS waste_usd,
               SUM(COALESCE(o.cache_read_tokens, 0))::float AS cache_read_tokens,
               SUM(COALESCE(o.prompt_tokens, 0))::float AS fresh_prompt_tokens,
               AVG((q.rubric->>'task_completion')::numeric)::float AS quality,
               AVG((q.rubric->>'irrelevant_share')::numeric)::float AS irrelevant_share
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
          LEFT JOIN nautgate.quality_evals q ON q.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(days => $1)
         GROUP BY 1
        HAVING COUNT(*) >= 5
         ORDER BY 2 DESC
        """, days)
    agents = []
    for r in rows:
        s = efficiency_score(dict(r))
        agents.append({"agent_id": r["agent_id"], "calls": r["calls"],
                       "cost_usd": round(r["cost_usd"] or 0, 4), **s})
    return {"days": days, "agents": agents, "weights": EFFICIENCY_WEIGHTS}


async def q_dataflow(pool: asyncpg.Pool, days: int) -> dict:
    rows = await pool.fetch(
        """
        SELECT d.agent_id, s->>'rule_id' AS rule_id, d.decision_provider AS provider,
               SUM(GREATEST(COALESCE((s->>'count')::int, 1), 1)) AS n
          FROM nautgate.route_decisions d,
               jsonb_array_elements(COALESCE(d.classified_signals, '[]'::jsonb)) s
         WHERE d.ts > NOW() - make_interval(days => $1)
         GROUP BY 1, 2, 3
        """, days)
    agent_cat: dict[tuple[str, str], int] = {}
    cat_prov: dict[tuple[str, str], int] = {}
    for r in rows:
        cat = CATEGORY.get(r["rule_id"], "other")
        n = int(r["n"])
        agent_cat[(r["agent_id"], cat)] = agent_cat.get((r["agent_id"], cat), 0) + n
        cat_prov[(cat, r["provider"])] = cat_prov.get((cat, r["provider"]), 0) + n
    return {
        "days": days,
        "agent_to_category": [{"source": a, "target": c, "value": v}
                              for (a, c), v in sorted(agent_cat.items(), key=lambda x: -x[1])],
        "category_to_provider": [{"source": c, "target": p, "value": v}
                                 for (c, p), v in sorted(cat_prov.items(), key=lambda x: -x[1])],
    }


async def q_overthinking(pool: asyncpg.Pool, limit: int = 800) -> dict:
    rows = await pool.fetch(
        """
        SELECT COALESCE(o.actual_model, d.decision_model) AS model,
               COALESCE(o.reasoning_tokens, 0) AS reasoning_tokens,
               o.completion_tokens,
               (q.rubric->>'task_completion')::float AS score
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
          JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE q.rubric ? 'task_completion'
           AND o.completion_tokens > 0
         ORDER BY q.ts DESC
         LIMIT $1
        """, limit)
    points = []
    for r in rows:
        total = int(r["completion_tokens"]) + int(r["reasoning_tokens"])
        points.append({
            "model": r["model"],
            "reasoning_share": round(int(r["reasoning_tokens"]) / total, 3),
            "score": r["score"],
        })
    return {"points": points}


# ── Dispatch table used by the route ────────────────────────────────────────


async def panel(name: str, pool: asyncpg.Pool, *, pricing=None,
                hours: int = 168, days: int = 7, metric: str = "completion_tokens",
                agent_id: str | None = None) -> Any:
    if name == "simulator":
        return await q_simulator(pool, pricing, hours)
    if name == "substitution":
        return await q_substitution(pool)
    if name == "spc":
        return await q_spc(pool, metric, hours)
    if name == "efficiency":
        return await q_efficiency(pool, days)
    if name == "dataflow":
        return await q_dataflow(pool, days)
    if name == "overthinking":
        return await q_overthinking(pool)
    if name == "tooling":
        return await q_tooling(pool, days, agent_id=agent_id)
    raise ValueError(f"unknown panel {name!r}")


# ── Audit report (Reports page) ─────────────────────────────────────────────


async def q_audit_report(pool: asyncpg.Pool, days: int) -> dict:
    """Everything the team LLM-usage & governance report needs, one payload.
    Read-only aggregates over the window; heavier than a panel, called on
    demand from the Reports page only."""
    totals = dict(await pool.fetchrow(
        """
        SELECT COUNT(*) AS calls,
               COUNT(DISTINCT d.agent_id) AS agents,
               COUNT(DISTINCT COALESCE(o.actual_model, d.decision_model)) AS models,
               COUNT(*) FILTER (WHERE o.status_code BETWEEN 200 AND 299) AS ok,
               COUNT(*) FILTER (WHERE o.status_code >= 400) AS errors,
               COALESCE(SUM(o.prompt_tokens), 0)::bigint AS prompt_tokens,
               COALESCE(SUM(o.completion_tokens), 0)::bigint AS completion_tokens,
               COALESCE(SUM(o.cache_read_tokens), 0)::bigint AS cache_read_tokens,
               COALESCE(SUM(NULLIF(o.cost_usd, 0)), 0)::float AS metered_usd,
               COALESCE(SUM(o.notional_cost_usd), 0)::float AS notional_usd,
               COALESCE(SUM(o.upstream_overload_retries), 0)::int AS retries_absorbed,
               COALESCE(SUM(d.estimated_waste_usd), 0)::float AS waste_usd
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(days => $1)
        """, days))
    agents = [dict(r) for r in await pool.fetch(
        """
        SELECT d.agent_id,
               COUNT(*) AS calls,
               COALESCE(SUM(COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0)), 0)::float AS spend_usd,
               COALESCE(SUM(o.prompt_tokens), 0)::bigint AS prompt_tokens,
               COALESCE(SUM(o.completion_tokens), 0)::bigint AS completion_tokens,
               (ARRAY_AGG(DISTINCT COALESCE(o.actual_model, d.decision_model)))[1:3] AS models,
               COUNT(*) FILTER (WHERE COALESCE(d.classified_sensitivity, 'none') != 'none') AS sensitive_calls,
               COALESCE(SUM(jsonb_array_length(COALESCE(d.classified_signals, '[]'::jsonb))), 0)::int AS findings,
               AVG((q.rubric->>'task_completion')::numeric)::float AS quality
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
          LEFT JOIN nautgate.quality_evals q ON q.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(days => $1)
         GROUP BY 1 ORDER BY 2 DESC
        """, days)]
    models = [dict(r) for r in await pool.fetch(
        """
        SELECT COALESCE(o.actual_model, d.decision_model) AS model,
               COUNT(*) AS calls,
               COALESCE(SUM(COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0)), 0)::float AS spend_usd
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(days => $1)
         GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """, days)]
    sensitivity = {r["s"]: int(r["n"]) for r in await pool.fetch(
        """
        SELECT COALESCE(classified_sensitivity, 'none') AS s, COUNT(*) AS n
          FROM nautgate.route_decisions
         WHERE ts > NOW() - make_interval(days => $1) GROUP BY 1
        """, days)}
    failure_tags = [dict(r) for r in await pool.fetch(
        """
        SELECT tag, COUNT(*) AS n
          FROM nautgate.quality_evals q, UNNEST(q.failure_tags) AS tag
         WHERE q.ts > NOW() - make_interval(days => $1)
         GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """, days)]
    drift_alerts = await pool.fetchval(
        "SELECT COUNT(*) FROM nautgate.drift_alerts WHERE started_at > NOW() - make_interval(days => $1)",
        days) if await pool.fetchval(
        "SELECT to_regclass('nautgate.drift_alerts') IS NOT NULL") else 0
    daily = [dict(r) for r in await pool.fetch(
        """
        SELECT date_trunc('day', d.ts)::date::text AS day,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE o.status_code >= 400) AS errors,
               COALESCE(SUM(COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0)), 0)::float AS spend_usd
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE d.ts > NOW() - make_interval(days => $1)
         GROUP BY 1 ORDER BY 1
        """, days)]
    dataflow = await q_dataflow(pool, days)
    from app import shadow as _shadow
    shadow_data = await _shadow.summary(pool, days=days)
    efficiency = await q_efficiency(pool, days)
    return {
        "days": days,
        "totals": totals,
        "agents": agents,
        "models": models,
        "sensitivity": sensitivity,
        "daily": daily,
        "failure_tags": failure_tags,
        "drift_alerts": int(drift_alerts or 0),
        "dataflow": dataflow,
        "shadow": {"experiments": shadow_data.get("experiments", [])},
        "efficiency": {a["agent_id"]: a for a in efficiency.get("agents", [])},
    }


# ── Improvements page (prompt coaching) ─────────────────────────────────────


async def q_improvements(pool: asyncpg.Pool, days: int, agent_id: str | None = None,
                         limit: int = 30) -> dict:
    """Coachable prompts + aggregated writing-style learnings.

    A prompt is coachable when the judge left a concrete suggested rewrite.
    Learnings only count evals where prompt_clarity <= 3 — i.e. the PROMPT was
    the problem, not the model (keeps model-failure anti-patterns out of the
    user's writing-habit stats)."""
    scope = "AND d.agent_id = $2" if agent_id else ""
    args: list = [days] + ([agent_id] if agent_id else [])
    prompts = [dict(r) for r in await pool.fetch(
        f"""
        SELECT d.id::text AS decision_id, d.ts, d.agent_id, d.prompt_excerpt,
               d.decision_model,
               (q.rubric->>'prompt_clarity')::int AS prompt_clarity,
               (q.rubric->>'task_completion')::int AS task_completion,
               q.anti_pattern, q.suggested_prompt, q.coach_notes, q.failure_tags,
               COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd)::float AS cost_usd,
               t.verdict AS sim_verdict, t.judge_reason AS sim_reason,
               t.challenger_cost_usd::float AS sim_cost_usd, t.id::text AS sim_trial_id
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
          LEFT JOIN LATERAL (
              SELECT id, verdict, judge_reason, challenger_cost_usd
                FROM nautgate.shadow_trials st
               WHERE st.decision_id = d.id AND st.trial_type = 'prompt_improve'
               ORDER BY st.ts DESC LIMIT 1) t ON true
         WHERE q.ts > NOW() - make_interval(days => $1)
           AND COALESCE(q.suggested_prompt, '') != ''
           AND COALESCE(d.classified_sensitivity, 'none') != 'secret'
           {scope}
         ORDER BY q.ts DESC LIMIT {int(limit)}
        """, *args)]
    for p in prompts:
        p["ts"] = p["ts"].isoformat()
    habits = [dict(r) for r in await pool.fetch(
        f"""
        SELECT q.anti_pattern, COUNT(*) AS n,
               MAX(q.suggested_prompt) AS example_fix
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
         WHERE q.ts > NOW() - make_interval(days => $1)
           AND COALESCE(q.anti_pattern, '') != ''
           AND (q.rubric->>'prompt_clarity')::int <= 3
           {scope}
         GROUP BY 1 ORDER BY 2 DESC LIMIT 8
        """, *args)]
    trend = [dict(r) for r in await pool.fetch(
        f"""
        SELECT date_trunc('week', q.ts)::date::text AS week,
               AVG((q.rubric->>'prompt_clarity')::numeric)::float AS clarity,
               COUNT(*) AS n
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
         WHERE q.ts > NOW() - make_interval(days => $1)
           AND q.rubric ? 'prompt_clarity'
           {scope}
         GROUP BY 1 ORDER BY 1
        """, *args)]
    return {"days": days, "agent_id": agent_id, "prompts": prompts,
            "habits": habits, "clarity_trend": trend}


async def q_headline(pool: asyncpg.Pool, pricing) -> dict:
    """Overview intelligence strip — the four headline numbers, one call."""
    eff = await q_efficiency(pool, 7)
    agents = eff.get("agents") or []
    tot_calls = sum(a["calls"] for a in agents) or 1
    eff_score = round(sum((a["score"] or 0) * a["calls"] for a in agents) / tot_calls) if agents else None
    sim = await q_simulator(pool, pricing, hours=168)
    best = max((p for p in sim["policies"] if p["priced_calls"] > 0),
               key=lambda p: p["savings_usd"], default=None)
    from app import shadow as _shadow
    cfg = await _shadow.shadow_config(pool)
    exps = (await _shadow.summary(pool, days=7)).get("experiments") or []
    live = [e for e in exps if e["n"] > 0]
    proven = [e for e in live if e.get("non_inferior") is True]
    sens = {r["s"]: int(r["n"]) for r in await pool.fetch(
        """SELECT COALESCE(classified_sensitivity,'none') AS s, COUNT(*) AS n
             FROM nautgate.route_decisions
            WHERE ts > NOW() - interval '7 days' GROUP BY 1""")}
    return {
        "efficiency": {"score": eff_score, "agents": len(agents)},
        "savings": ({"weekly_usd": round(best["savings_usd"], 2), "policy": best["policy"]}
                    if best and best["savings_usd"] > 0 else None),
        "experiments": {"count": len(live), "proven": len(proven),
                        "running": bool(cfg.get("enabled") or cfg.get("diet_enabled"))},
        "governance": {"secrets": sens.get("secret", 0), "pii": sens.get("pii", 0),
                       "clean": sens.get("none", 0)},
    }


# ── Tooling (MCP impact) ────────────────────────────────────────────────────

# The classic filesystem discovery loop — what a graph/MCP call replaces.
FS_DISCOVERY_TOOLS = ("Read", "Grep", "Glob", "LS")


async def q_tooling(pool: asyncpg.Pool, days: int, agent_id: str | None = None) -> dict:
    """MCP/tool economics from captured traffic: what each connected server
    costs to carry (schema payload), how much it's used, and the daily
    discovery mix (filesystem crawling vs MCP answers) with avg input tokens —
    the 'seeing faster = cheaper' evidence."""
    usage_rows = await pool.fetch(
        """
        SELECT c->>'name' AS tool, COUNT(*) AS n, MAX(o.ts) AS last_used
          FROM nautgate.route_outcomes o
          JOIN nautgate.route_decisions d ON d.id = o.decision_id,
               jsonb_array_elements(o.tool_calls_made) c
         WHERE o.ts > NOW() - make_interval(days => $1)
           AND jsonb_typeof(o.tool_calls_made) = 'array'
           AND ($2::text IS NULL OR d.agent_id = $2)
         GROUP BY 1 ORDER BY 2 DESC
        """, days, agent_id)
    servers: dict[str, dict] = {}
    for r in usage_rows:
        name = r["tool"] or "?"
        server = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else "built-in"
        s = servers.setdefault(server, {"server": server, "invocations": 0, "tools_used": []})
        s["invocations"] += int(r["n"])
        s["tools_used"].append({"tool": name, "n": int(r["n"]),
                                "last_used": r["last_used"].isoformat()})
    # Schema overhead: latest captured tool manifest per agent; a server's
    # footprint is what its tool defs weigh in EVERY request that carries them.
    manifests = await pool.fetch(
        """
        SELECT DISTINCT ON (agent_id) agent_id, tools_body
          FROM nautgate.route_decisions
         WHERE tools_body IS NOT NULL AND ts > NOW() - make_interval(days => $1)
           AND ($2::text IS NULL OR agent_id = $2)
         ORDER BY agent_id, ts DESC LIMIT 12
        """, days, agent_id)
    import json as _json
    overhead: dict[str, dict] = {}
    for m in manifests:
        try:
            defs = _json.loads(m["tools_body"])
        except (ValueError, TypeError):
            continue
        if not isinstance(defs, list):
            continue
        for d in defs:
            if not isinstance(d, dict):
                continue
            name = d.get("name") or "?"
            server = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else "built-in"
            o = overhead.setdefault(server, {"tools": set(), "bytes": 0, "agents": set()})
            o["agents"].add(m["agent_id"])
            if name not in o["tools"]:
                o["tools"].add(name)
                o["bytes"] += len(_json.dumps(d, ensure_ascii=False).encode("utf-8"))
    for server, o in overhead.items():
        s = servers.setdefault(server, {"server": server, "invocations": 0, "tools_used": []})
        s["tool_count"] = len(o["tools"])
        s["schema_tokens"] = round(o["bytes"] / 4)  # char/4 estimate, same as audit_meta
        s["agents_carrying"] = len(o["agents"])
    out_servers = sorted(servers.values(), key=lambda s: -s["invocations"])
    for s in out_servers:
        s["tools_used"] = s["tools_used"][:8]
    daily = [dict(r) for r in await pool.fetch(
        r"""
        SELECT date_trunc('day', o.ts)::date::text AS day,
               COUNT(*) AS calls,
               AVG(o.prompt_tokens)::float AS avg_prompt_tokens,
               COALESCE(SUM((SELECT COUNT(*) FROM jsonb_array_elements(o.tool_calls_made) c
                     WHERE c->>'name' = ANY($2::text[]))), 0)::int AS fs_calls,
               COALESCE(SUM((SELECT COUNT(*) FROM jsonb_array_elements(o.tool_calls_made) c
                     WHERE c->>'name' LIKE 'mcp\_\_%')), 0)::int AS mcp_calls
          FROM nautgate.route_outcomes o
          JOIN nautgate.route_decisions d ON d.id = o.decision_id
         WHERE o.ts > NOW() - make_interval(days => $1)
           AND jsonb_typeof(o.tool_calls_made) = 'array'
           AND ($3::text IS NULL OR d.agent_id = $3)
         GROUP BY 1 ORDER BY 1
        """, days, list(FS_DISCOVERY_TOOLS), agent_id)]
    return {"days": days, "agent_id": agent_id, "servers": out_servers, "daily": daily,
            "fs_tools": list(FS_DISCOVERY_TOOLS)}


# ── Usage-burst detection (notifications) ───────────────────────────────────


async def q_bursts(pool: asyncpg.Pool) -> list[dict]:
    """Sudden usage bursts: the CURRENT hour's calls/spend per agent vs that
    agent's trailing 7-day hourly median. Fires at >4× median (with floors so
    quiet agents don't alert on noise). A runaway loop shows up here within
    the hour it starts."""
    rows = await pool.fetch(
        """
        WITH hourly AS (
          SELECT d.agent_id, date_trunc('hour', d.ts) AS h,
                 COUNT(*) AS calls,
                 COALESCE(SUM(COALESCE(NULLIF(o.cost_usd, 0), o.notional_cost_usd, 0)), 0) AS spend
            FROM nautgate.route_decisions d
            LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
           WHERE d.ts > NOW() - interval '7 days'
           GROUP BY 1, 2
        ), base AS (
          SELECT agent_id,
                 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY calls) AS med_calls,
                 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spend) AS med_spend
            FROM hourly
           WHERE h < date_trunc('hour', NOW())
           GROUP BY agent_id
          HAVING COUNT(*) >= 12   -- need a real baseline before judging
        )
        SELECT h.agent_id, h.calls::int, h.spend::float,
               b.med_calls::float, b.med_spend::float
          FROM hourly h JOIN base b USING (agent_id)
         WHERE h.h = date_trunc('hour', NOW())
           AND (h.calls > GREATEST(20, 4 * b.med_calls)
                OR h.spend > GREATEST(1.0, 4 * b.med_spend))
         ORDER BY h.calls DESC
        """)
    return [dict(r) for r in rows]
