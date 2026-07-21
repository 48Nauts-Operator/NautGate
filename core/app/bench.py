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

import asyncpg
import httpx
import structlog

from app.shadow import _pricing_provider, call_challenger

log = structlog.get_logger()

MAX_BENCH_MODELS = 4

# Canned tool set for "does this model reach for tools?" tests. OpenAI format
# (converted for Anthropic transports automatically).
SAMPLE_TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the project by path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Repo-relative file path"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_code",
        "description": "Search the codebase for a pattern.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command and return its output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
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
    return ("openrouter", f"openrouter/{t}")


async def run_bench(pool: asyncpg.Pool, *, pricing, client: httpx.AsyncClient,
                    agent_id: str, prompt: str, models: list[str],
                    tools: list[dict] | None = None, max_tokens: int = 1000) -> dict:
    """Fan the task out, persist, return the run."""
    models = [m for m in models if (m or "").strip()][:MAX_BENCH_MODELS]
    messages = [{"role": "user", "content": prompt}]

    async def one(target: str) -> dict:
        provider, model = _split_target(target)
        res = await call_challenger(client, provider, model, messages, tools=tools)
        cost = None
        if pricing is not None and res.get("prompt_tokens") is not None:
            cost = pricing.compute_cost(
                _pricing_provider(provider, model), model,
                prompt_tokens=res.get("prompt_tokens"),
                completion_tokens=res.get("completion_tokens"))
        return {
            "target": target, "provider": provider, "model": model,
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
        agent_id, prompt, json.dumps(tools) if tools else None,
        max_tokens, json.dumps(list(results)),
    )
    return {"id": row["id"], "ts": row["ts"].isoformat(), "prompt": prompt,
            "tools": tools, "results": list(results)}


async def recent_runs(pool: asyncpg.Pool, limit: int = 10) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id::text, ts, prompt, tools, results
          FROM nautgate.bench_runs ORDER BY ts DESC LIMIT $1
        """, min(limit, 50))
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


async def working_compare(pool: asyncpg.Pool, *, hours: int = 24,
                          agent_id: str | None = None) -> dict:
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
        """, hours, agent_id)
    return {"hours": hours, "agent_id": agent_id,
            "models": [dict(r) for r in rows]}
