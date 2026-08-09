"""Model test bench — one task, N models, side by side.

Two modes, both read-only with respect to the routing path:

  run_bench       — synthetic: send the SAME prompt (optionally with tool
                    definitions) to each selected model in parallel; record
                    text, tool calls WANTED (never executed), tokens, cost,
                    latency. One row in nautgate.bench_runs per run.
  working_compare — passive: profile the models that served REAL traffic in a
                    window (calls, tool calls/turn, tokens, cost, latency) so
                    switching models mid-work shows the behavioral difference
                    without any synthetic calls.
"""

from __future__ import annotations

import asyncio
import json
import re

import asyncpg
import httpx
import structlog

from app.shadow import _pricing_provider, call_challenger

log = structlog.get_logger()

MAX_BENCH_MODELS = 4

# Canned tool set for "does this model reach for tools?" tests. OpenAI format
# (converted for Anthropic transports automatically).
SAMPLE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project by path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the codebase for a pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def _split_target(target: str) -> tuple[str, str]:
    """'anthropic/claude-haiku-4-5' → (provider, model); bare model → anthropic
    for claude-*, openrouter otherwise (openrouter models keep their ns)."""
    t = (target or "").strip()
    if t.startswith("openrouter/"):
        return ("openrouter", t)
    if "/" in t:
        provider, model = t.split("/", 1)
        return (provider, model)
    if t.startswith("claude"):
        return ("anthropic", t)
    # Bare OpenAI ids go direct to OpenAI. Without this they fell through to
    # the openrouter branch below as "openrouter/gpt-5.6-sol" — an id that
    # doesn't exist there, so the bench leg failed instead of running.
    if re.match(r"^(gpt-|o1|o3|o4|chatgpt-)", t):
        return ("openai", t)
    return ("openrouter", f"openrouter/{t}")


async def run_bench(
    pool: asyncpg.Pool,
    *,
    pricing,
    client: httpx.AsyncClient,
    agent_id: str,
    prompt: str,
    models: list[str],
    tools: list[dict] | None = None,
    max_tokens: int = 1000,
) -> dict:
    """Fan the task out, persist, return the run."""
    models = [m for m in models if (m or "").strip()][:MAX_BENCH_MODELS]
    messages = [{"role": "user", "content": prompt}]

    async def one(target: str) -> dict:
        provider, model = _split_target(target)
        res = await call_challenger(client, provider, model, messages, tools=tools)
        cost = None
        if pricing is not None and res.get("prompt_tokens") is not None:
            cost = pricing.compute_cost(
                _pricing_provider(provider, model),
                model,
                prompt_tokens=res.get("prompt_tokens"),
                completion_tokens=res.get("completion_tokens"),
            )
        return {
            "target": target,
            "provider": provider,
            "model": model,
            "status": res.get("status"),
            "via_fallback": res.get("via_fallback"),
            "latency_ms": res.get("latency_ms"),
            "prompt_tokens": res.get("prompt_tokens"),
            "completion_tokens": res.get("completion_tokens"),
            "cost_usd": cost,
            "tool_calls": res.get("tool_calls") or [],
            "text": (res.get("text") or "")[:6000],
            "error": (res.get("error") or "")[:300] or None,
        }

    results = await asyncio.gather(*(one(m) for m in models))
    row = await pool.fetchrow(
        """
        INSERT INTO nautgate.bench_runs (agent_id, prompt, tools, max_tokens, results)
        VALUES ($1, $2, $3::jsonb, $4, $5::jsonb)
        RETURNING id::text, ts
        """,
        agent_id,
        prompt,
        json.dumps(tools) if tools else None,
        max_tokens,
        json.dumps(list(results)),
    )
    return {
        "id": row["id"],
        "ts": row["ts"].isoformat(),
        "prompt": prompt,
        "tools": tools,
        "results": list(results),
    }


async def recent_runs(pool: asyncpg.Pool, limit: int = 10) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id::text, ts, prompt, tools, results
          FROM nautgate.bench_runs ORDER BY ts DESC LIMIT $1
        """,
        min(limit, 50),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["ts"] = d["ts"].isoformat()
        for k in ("tools", "results"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    pass
        out.append(d)
    return out


async def working_compare(
    pool: asyncpg.Pool, *, hours: int = 24, agent_id: str | None = None
) -> dict:
    """Per-model behavioral profile from REAL traffic in the window — the
    'just while working' comparison. No synthetic calls."""
    rows = await pool.fetch(
        """
        SELECT COALESCE(o.actual_model, d.decision_model) AS model,
               COUNT(*) AS calls,
               AVG(CASE WHEN jsonb_typeof(o.tool_calls_made) = 'array'
                   THEN jsonb_array_length(o.tool_calls_made) ELSE 0 END)::float AS tool_calls_per_turn,
               AVG(o.prompt_tokens)::float AS fresh_in_per_call,
               AVG(o.completion_tokens)::float AS out_per_call,
               AVG(CASE WHEN COALESCE(o.completion_tokens,0)+COALESCE(o.reasoning_tokens,0) > 0
                   THEN COALESCE(o.reasoning_tokens,0)::float
                        / (COALESCE(o.completion_tokens,0)+COALESCE(o.reasoning_tokens,0)) END)::float AS thinking_share,
               AVG(COALESCE(NULLIF(o.cost_usd,0), o.notional_cost_usd))::float AS cost_per_call,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.duration_ms)::float AS p50_ms
          FROM nautgate.route_outcomes o
          JOIN nautgate.route_decisions d ON d.id = o.decision_id
         WHERE o.ts > NOW() - make_interval(hours => $1)
           AND o.status_code BETWEEN 200 AND 299
           AND ($2::text IS NULL OR d.agent_id = $2)
         GROUP BY 1
        HAVING COUNT(*) >= 3
         ORDER BY 2 DESC LIMIT 6
        """,
        hours,
        agent_id,
    )
    return {"hours": hours, "agent_id": agent_id, "models": [dict(r) for r in rows]}


async def head_to_head(
    pool: asyncpg.Pool, *, hours: int = 24, agent_id: str | None = None, min_models: int = 2
) -> dict:
    """Pair REAL calls that answered the SAME task, one row per model.

    `working_compare` averages a model over all its traffic, which is
    misleading when the same model runs in two roles: a 57-tool builder session
    and a 5-tool opinion call land in one bucket, so tool-schema overhead looks
    like model inefficiency. Here we group by the task (the user prompt) and
    only keep tasks that >= `min_models` distinct models actually answered, so
    every comparison is like-for-like.

    Correlation key is md5(prompt_excerpt) — the harness sends both models the
    same question, so the excerpt matches with no client change and no schema
    change. ponytail: excerpt-hash beats a new correlation column until a
    caller needs to pair tasks whose first 200 chars collide.
    """
    rows = await pool.fetch(
        """
        WITH calls AS (
            SELECT md5(d.prompt_excerpt) AS task_hash,
                   d.prompt_excerpt       AS excerpt,
                   COALESCE(o.actual_model, d.decision_model) AS model,
                   d.tools_count, o.prompt_tokens, o.completion_tokens,
                   o.reasoning_tokens, o.cost_usd, o.duration_ms,
                   o.first_byte_ms, o.ts,
                   CASE WHEN jsonb_typeof(o.tool_calls_made) = 'array'
                        THEN o.tool_calls_made ELSE '[]'::jsonb END AS tcalls
              FROM nautgate.route_outcomes o
              JOIN nautgate.route_decisions d ON d.id = o.decision_id
             WHERE o.ts > NOW() - make_interval(hours => $1)
               AND o.status_code BETWEEN 200 AND 299
               AND COALESCE(d.prompt_excerpt, '') <> ''
               AND ($2::text IS NULL OR d.agent_id = $2)
        ), per_model AS (
            SELECT task_hash, MIN(excerpt) AS excerpt, model,
                   COUNT(*)                          AS calls,
                   MIN(ts)                           AS first_ts,
                   AVG(tools_count)::float           AS tools_offered,
                   SUM(prompt_tokens)                AS tokens_in,
                   SUM(completion_tokens)            AS tokens_out,
                   SUM(reasoning_tokens)             AS tokens_reasoning,
                   SUM(cost_usd)::float              AS cost_usd,
                   COUNT(cost_usd)                   AS priced_calls,
                   AVG(first_byte_ms)::float         AS ttfb_ms,
                   SUM(duration_ms)                  AS duration_ms,
                   SUM(jsonb_array_length(tcalls))   AS tool_calls,
                   COALESCE(jsonb_agg(DISTINCT tc->>'name')
                            FILTER (WHERE tc->>'name' IS NOT NULL),
                            '[]'::jsonb)             AS tool_names
              FROM calls LEFT JOIN LATERAL jsonb_array_elements(tcalls) tc ON TRUE
             GROUP BY task_hash, model
        )
        SELECT * FROM per_model
         WHERE task_hash IN (SELECT task_hash FROM per_model
                             GROUP BY task_hash HAVING COUNT(DISTINCT model) >= $3)
         ORDER BY first_ts DESC, model
        """,
        hours,
        agent_id,
        min_models,
    )

    tasks: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        t = tasks.setdefault(
            d["task_hash"],
            {
                "task_hash": d["task_hash"],
                "excerpt": d["excerpt"],
                "first_ts": d["first_ts"].isoformat() if d.get("first_ts") else None,
                "models": [],
            },
        )
        tin, tout = d.get("tokens_in") or 0, d.get("tokens_out") or 0
        t["models"].append(
            {
                "model": d["model"],
                "calls": d["calls"],
                "tools_offered": d["tools_offered"],
                "tokens_in": tin,
                "tokens_out": tout,
                "tokens_reasoning": d.get("tokens_reasoning") or 0,
                # context efficiency: input tokens burned per output token produced.
                "in_per_out": round(tin / tout, 2) if tout else None,
                "cost_usd": d.get("cost_usd"),
                # unpriced models must read as UNKNOWN, never as $0 — a missing
                # pricing.yaml entry otherwise looks like a free model.
                "unpriced": (d.get("priced_calls") or 0) < d["calls"],
                "ttfb_ms": d.get("ttfb_ms"),
                "duration_ms": d.get("duration_ms"),
                "tool_calls": d.get("tool_calls") or 0,
                "tool_names": json.loads(d["tool_names"]) if d.get("tool_names") else [],
            }
        )
    return {"hours": hours, "agent_id": agent_id, "tasks": list(tasks.values())}


def _qualify(model: str) -> str | None:
    """Bare model id → a provider-qualified bench target `_split_target` can
    route. Returns None when we can't tell who serves it (offering an
    unroutable target is worse than omitting it)."""
    m = (model or "").strip()
    if not m or m == "auto":
        return None
    if "/" in m:
        return m
    if m.startswith("claude"):
        return f"anthropic/{m}"
    if re.match(r"^(gpt-|o1|o3|o4|chatgpt-)", m):
        return f"openai/{m}"
    return None


async def available_models(pool: asyncpg.Pool, pricing=None) -> list[str]:
    """Bench targets to offer in the UI: everything we have a price for, plus
    everything that actually served traffic recently.

    The old UI hardcoded this list, so newly-adopted models (gpt-5.6-*,
    claude-fable-5) were simply unpickable. Deriving it keeps the picker
    honest as models come and go.
    """
    # Only these providers have a transport in _select_transports; anything
    # else (gemini/*, deepseek/*, lmstudio/*) would resolve to "no_transport"
    # and fail the leg, so it must not be offered.
    ROUTABLE = ("anthropic/", "openai/", "openrouter/")

    def usable(t: str) -> bool:
        return (
            t.startswith(ROUTABLE)
            and not t.endswith("]")  # pricing aliases like "…[1m]"
            and not t.endswith("/local")
        )  # lmstudio / local stubs

    targets: set[str] = set()
    for key in getattr(pricing, "_prices", {}):
        if usable(key) and not key.startswith("openrouter/openrouter/"):
            targets.add(key)
    try:
        rows = await pool.fetch(
            """
            SELECT DISTINCT COALESCE(o.actual_model, d.decision_model) AS m
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.ts > NOW() - INTERVAL '30 days'
            """
        )
        for r in rows:
            q = _qualify(r["m"])
            if q and usable(q):
                targets.add(q)
    except Exception as exc:  # noqa: BLE001 - picker must not break the page
        log.warning(
            "bench_available_models_failed",
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
    return sorted(targets)
