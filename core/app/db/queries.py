"""Query layer. Synchronous PRECAPTURE writes (audit log) and outcome writes.

The ledger durability contract (Tech Paper §9) requires `route_decisions` to be written
synchronously *before* upstream forward; `route_outcomes` is also synchronous on healthy
DB and gets a durable-spool fallback (Day 4d).
"""

import json
from datetime import UTC
from uuid import UUID

import asyncpg

from app.audit_receipt import finalized_receipt


async def precapture(
    pool: asyncpg.Pool,
    *,
    decision_id: UUID,
    agent_id: str,
    inbound_format: str,
    model_requested: str | None,
    classified_tier: str,
    decision_provider: str,
    decision_model: str,
    decision_reason: str | None = None,
    prompt_excerpt: str | None = None,
    prompt_tokens: int | None = None,
    classified_sensitivity: str | None = None,
    classified_signals: list[dict] | None = None,
    classified_score: float | None = None,
    prompt_body: str | None = None,
    prompt_body_truncated_at_byte: int | None = None,
    tools_body: str | None = None,
    tools_body_truncated_at_byte: int | None = None,
    brain_hints: dict | None = None,
    source_ip: str | None = None,
    source_hostname: str | None = None,
    messages_count: int | None = None,
    tools_count: int | None = None,
    stream_flag: bool | None = None,
    request_size_bytes: int | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Insert the audit row before forwarding upstream. Synchronous by design."""
    signals_json = json.dumps(classified_signals) if classified_signals else None
    brain_hints_json = json.dumps(brain_hints) if brain_hints else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.route_decisions
                (id, agent_id, inbound_format, model_requested, prompt_excerpt,
                 prompt_tokens, classified_tier, classified_score,
                 classified_sensitivity, classified_signals, brain_hints,
                 decision_provider, decision_model, decision_reason,
                 prompt_body, prompt_body_truncated_at_byte,
                 tools_body, tools_body_truncated_at_byte,
                 source_ip, source_hostname, messages_count, tools_count,
                 stream_flag, request_size_bytes, session_id, project_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb,
                    $12, $13, $14, $15, $16,
                    $17, $18,
                    $19::inet, $20, $21, $22, $23, $24, $25, $26)
            """,
            decision_id,
            agent_id,
            inbound_format,
            model_requested,
            prompt_excerpt,
            prompt_tokens,
            classified_tier,
            classified_score,
            classified_sensitivity,
            signals_json,
            brain_hints_json,
            decision_provider,
            decision_model,
            decision_reason,
            prompt_body,
            prompt_body_truncated_at_byte,
            tools_body,
            tools_body_truncated_at_byte,
            source_ip,
            source_hostname,
            messages_count,
            tools_count,
            stream_flag,
            request_size_bytes,
            session_id,
            project_id,
        )


async def write_outcome(
    pool: asyncpg.Pool,
    *,
    decision_id: UUID,
    status_code: int,
    duration_ms: int,
    first_byte_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    prefix_hash: str | None = None,
    cost_usd: float | None = None,
    was_empty: bool = False,
    used_fallback: bool = False,
    fallback_count: int = 0,
    client_disconnected: bool = False,
    was_truncated: bool = False,
    truncated_at_byte: int | None = None,
    response_body: str | None = None,
    response_body_truncated_at_byte: int | None = None,
    response_size_bytes: int | None = None,
    tool_calls_made: list[dict] | None = None,
    actual_model: str | None = None,
    actual_provider: str | None = None,
    notional_cost_usd: float | None = None,
    rate_limited_429: bool = False,
    upstream_overload_retries: int = 0,
    evidence: dict | None = None,
) -> None:
    tool_calls_json = json.dumps(tool_calls_made) if tool_calls_made else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            outcome_row = await conn.fetchrow(
                """
            INSERT INTO nautgate.route_outcomes
                (decision_id, status_code, duration_ms, first_byte_ms,
                 prompt_tokens, completion_tokens, reasoning_tokens,
                 cache_read_tokens, cache_write_tokens, prefix_hash, cost_usd,
                 was_empty, used_fallback, fallback_count, client_disconnected,
                 was_truncated, truncated_at_byte,
                 response_body, response_body_truncated_at_byte,
                 response_size_bytes, tool_calls_made,
                 actual_model, actual_provider,
                 notional_cost_usd, rate_limited_429, upstream_overload_retries)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                    $18, $19, $20, $21::jsonb, $22, $23, $24, $25, $26)
            RETURNING ts
            """,
                decision_id,
                status_code,
                duration_ms,
                first_byte_ms,
                prompt_tokens,
                completion_tokens,
                reasoning_tokens,
                cache_read_tokens,
                cache_write_tokens,
                prefix_hash,
                cost_usd,
                was_empty,
                used_fallback,
                fallback_count,
                client_disconnected,
                was_truncated,
                truncated_at_byte,
                response_body,
                response_body_truncated_at_byte,
                response_size_bytes,
                tool_calls_json,
                actual_model,
                actual_provider,
                notional_cost_usd,
                rate_limited_429,
                upstream_overload_retries,
            )
            decision_row = await conn.fetchrow(
                """
                SELECT id, ts, agent_id, inbound_format, model_requested,
                       classified_sensitivity, classified_signals,
                       decision_provider, decision_model, decision_reason,
                       fallback_chain, stream_flag
                  FROM nautgate.route_decisions
                 WHERE id = $1
                """,
                decision_id,
            )
            if decision_row is None:
                raise RuntimeError(f"route decision disappeared before outcome: {decision_id}")
            sequence = await conn.fetchval("SELECT nextval('nautgate.audit_receipt_sequence')")
            receipt_id = UUID(bytes=__import__("os").urandom(16), version=4)
            outcome = {
                "ts": outcome_row["ts"],
                "status_code": status_code,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "actual_model": actual_model,
                "actual_provider": actual_provider,
                "tool_calls_made": tool_calls_made,
            }
            receipt, canonical, digest = finalized_receipt(
                sequence=sequence,
                receipt_id=receipt_id,
                decision=dict(decision_row),
                outcome=outcome,
                evidence=evidence,
            )
            await conn.execute(
                """
                INSERT INTO nautgate.audit_receipts
                    (receipt_id, decision_id, evidence_sequence, schema_version,
                     canonical_receipt, canonical_bytes, receipt_hash)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                """,
                receipt_id,
                decision_id,
                sequence,
                receipt["schema"],
                json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
                canonical,
                digest,
            )
            await conn.execute(
                """
                INSERT INTO nautgate.audit_outbox (receipt_id, evidence_sequence)
                VALUES ($1, $2)
                """,
                receipt_id,
                sequence,
            )


async def get_routing_preferences(pool: asyncpg.Pool, *, agent_id: str) -> dict:
    """Return the routing_preferences row for `agent_id`, or empty defaults."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT agent_id, preferred_tier_overrides, banned_models, preferred_models,
                   notes, updated_at
              FROM nautgate.routing_preferences
             WHERE agent_id = $1
            """,
            agent_id,
        )
    if row is None:
        return {
            "agent_id": agent_id,
            "preferred_tier_overrides": None,
            "banned_models": [],
            "preferred_models": [],
            "notes": None,
            "updated_at": None,
        }
    overrides = row["preferred_tier_overrides"]
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    return {
        "agent_id": row["agent_id"],
        "preferred_tier_overrides": overrides,
        "banned_models": list(row["banned_models"] or []),
        "preferred_models": list(row["preferred_models"] or []),
        "notes": row["notes"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def upsert_routing_preferences(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    preferred_tier_overrides: dict | None = None,
    banned_models: list[str] | None = None,
    preferred_models: list[str] | None = None,
    notes: str | None = None,
) -> dict:
    """UPSERT a routing_preferences row. Returns the resulting row in get_*-shape."""
    overrides_json = json.dumps(preferred_tier_overrides) if preferred_tier_overrides else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.routing_preferences
                (agent_id, preferred_tier_overrides, banned_models, preferred_models, notes,
                 updated_at)
            VALUES ($1, $2::jsonb, $3, $4, $5, NOW())
            ON CONFLICT (agent_id) DO UPDATE SET
                preferred_tier_overrides = EXCLUDED.preferred_tier_overrides,
                banned_models = EXCLUDED.banned_models,
                preferred_models = EXCLUDED.preferred_models,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """,
            agent_id,
            overrides_json,
            list(banned_models) if banned_models is not None else None,
            list(preferred_models) if preferred_models is not None else None,
            notes,
        )
    return await get_routing_preferences(pool, agent_id=agent_id)


async def get_cost_summary(
    pool: asyncpg.Pool,
    *,
    agent_id: str | None,
    hours: int,
    project_id: str | None = None,
) -> dict:
    """Aggregate cost over the last N hours, broken down by provider/model/tier.

    ``agent_id=None`` or ``"*"`` returns aggregate across all agents.
    Any other value filters to that single agent.
    ``project_id`` further narrows to one project (cost center).
    """
    is_all = agent_id is None or agent_id == "*"
    conds: list[str] = ["d.ts > NOW() - make_interval(hours => $1)"]
    base_params: list = [hours]
    if not is_all:
        base_params.append(agent_id)
        conds.append(f"d.agent_id = ${len(base_params)}")
    if project_id and project_id != "*":
        base_params.append(project_id)
        conds.append(f"d.project_id = ${len(base_params)}")
    where = " AND ".join(conds)

    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            f"""
            SELECT COUNT(*)                          AS total_calls,
                   SUM(o.cost_usd)::FLOAT            AS total_cost_usd,
                   SUM(o.notional_cost_usd)::FLOAT   AS subscription_savings_usd,
                   SUM(o.prompt_tokens)::BIGINT      AS total_prompt_tokens,
                   SUM(o.completion_tokens)::BIGINT  AS total_completion_tokens,
                   SUM(CASE WHEN o.rate_limited_429 THEN 1 ELSE 0 END)
                       AS rate_limited_count,
                   SUM(CASE WHEN o.was_empty THEN 1 ELSE 0 END)
                       AS empty_count
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE {where}
            """,
            *base_params,
        )

        async def _by(field_sql: str):
            rows = await conn.fetch(
                f"""
                SELECT {field_sql}                   AS k,
                       SUM(o.cost_usd)::FLOAT        AS cost_usd,
                       SUM(o.notional_cost_usd)::FLOAT AS notional_cost_usd,
                       COUNT(*)                       AS calls,
                       SUM(o.prompt_tokens)::BIGINT  AS prompt_tokens,
                       SUM(o.completion_tokens)::BIGINT AS completion_tokens,
                       AVG(o.duration_ms)::FLOAT     AS avg_latency_ms,
                       SUM(CASE WHEN o.was_empty THEN 1 ELSE 0 END) AS empty_count
                  FROM nautgate.route_decisions d
                  LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
                 WHERE {where}
                 GROUP BY {field_sql}
                 ORDER BY COALESCE(SUM(o.cost_usd), 0) + COALESCE(SUM(o.notional_cost_usd), 0) DESC NULLS LAST
                """,
                *base_params,
            )
            return [
                {
                    "key": r["k"],
                    "cost_usd": r["cost_usd"],
                    "notional_cost_usd": r["notional_cost_usd"],
                    "calls": int(r["calls"]),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                    "avg_latency_ms": (
                        int(r["avg_latency_ms"]) if r["avg_latency_ms"] is not None else None
                    ),
                    "empty_count": int(r["empty_count"] or 0),
                }
                for r in rows
            ]

        by_provider = await _by("d.decision_provider")
        by_model = await _by("d.decision_model")
        by_tier = await _by("d.classified_tier")
        # New: per-agent breakdown only useful on the "all" view.
        by_agent = await _by("d.agent_id") if is_all else []
        # Per-project breakdown — only meaningful when not already filtered.
        by_project = (
            await _by("COALESCE(d.project_id, '(none)')")
            if not project_id or project_id == "*"
            else []
        )

        # Overview "streams": one agent with the models/providers it actually
        # used.  Keep this linked at query time; combining the independent
        # by_agent + by_model lists in the browser cannot tell which agent used
        # which model and produced a misleading cost view.
        stream_rows = await conn.fetch(
            f"""
            SELECT d.agent_id AS agent_id,
                   COALESCE(o.actual_provider, d.decision_provider) AS provider,
                   COALESCE(o.actual_model, d.decision_model) AS model,
                   COUNT(*) AS calls,
                   SUM(o.cost_usd)::FLOAT AS cost_usd,
                   SUM(o.notional_cost_usd)::FLOAT AS notional_cost_usd,
                   SUM(o.prompt_tokens)::BIGINT AS prompt_tokens,
                   SUM(o.completion_tokens)::BIGINT AS completion_tokens,
                   MAX(d.ts) AS last_seen_at,
                   SUM(CASE
                         WHEN COALESCE(o.cost_usd, 0) > 0 THEN 1 ELSE 0
                       END) AS metered_calls,
                   SUM(CASE
                         WHEN COALESCE(o.cost_usd, 0) = 0
                          AND COALESCE(o.notional_cost_usd, 0) > 0
                         THEN 1 ELSE 0
                       END) AS subscription_calls,
                   SUM(CASE
                         WHEN COALESCE(o.actual_provider, d.decision_provider) = 'lmstudio'
                           OR COALESCE(o.actual_model, d.decision_model) LIKE 'lmstudio/%'
                         THEN 1 ELSE 0
                       END) AS local_calls
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE {where}
             GROUP BY d.agent_id,
                      COALESCE(o.actual_provider, d.decision_provider),
                      COALESCE(o.actual_model, d.decision_model)
             ORDER BY MAX(d.ts) DESC
            """,
            *base_params,
        )

        streams_by_agent: dict[str, dict] = {}
        for r in stream_rows:
            agent = r["agent_id"] or "unknown"
            stream = streams_by_agent.setdefault(
                agent,
                {
                    "agent_id": agent,
                    "calls": 0,
                    "cost_usd": 0.0,
                    "notional_cost_usd": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "metered_calls": 0,
                    "subscription_calls": 0,
                    "local_calls": 0,
                    "last_seen_at": None,
                    "models": [],
                },
            )
            calls = int(r["calls"] or 0)
            cost = float(r["cost_usd"] or 0)
            notional = float(r["notional_cost_usd"] or 0)
            stream["calls"] += calls
            stream["cost_usd"] += cost
            stream["notional_cost_usd"] += notional
            stream["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            stream["completion_tokens"] += int(r["completion_tokens"] or 0)
            stream["metered_calls"] += int(r["metered_calls"] or 0)
            stream["subscription_calls"] += int(r["subscription_calls"] or 0)
            stream["local_calls"] += int(r["local_calls"] or 0)
            seen = r["last_seen_at"]
            if seen and (stream["last_seen_at"] is None or seen > stream["last_seen_at"]):
                stream["last_seen_at"] = seen
            stream["models"].append(
                {
                    "provider": r["provider"],
                    "model": r["model"],
                    "calls": calls,
                    "cost_usd": cost,
                    "notional_cost_usd": notional,
                }
            )

        by_stream = list(streams_by_agent.values())
        for stream in by_stream:
            seen = stream["last_seen_at"]
            if seen:
                stream["last_seen_at"] = seen.isoformat()
            stream["models"].sort(key=lambda m: m["calls"], reverse=True)
            classified = (
                stream["metered_calls"] + stream["subscription_calls"] + stream["local_calls"]
            )
            stream["unpriced_calls"] = max(0, stream["calls"] - classified)
        by_stream.sort(
            key=lambda r: (
                r["cost_usd"] + r["notional_cost_usd"],
                r["calls"],
            ),
            reverse=True,
        )

    return {
        "agent_id": "*" if is_all else agent_id,
        "project_id": project_id or "*",
        "window_hours": hours,
        "total_calls": int((totals or {}).get("total_calls") or 0),
        "total_cost_usd": (totals or {}).get("total_cost_usd"),
        "subscription_savings_usd": (totals or {}).get("subscription_savings_usd"),
        "total_prompt_tokens": int((totals or {}).get("total_prompt_tokens") or 0),
        "total_completion_tokens": int((totals or {}).get("total_completion_tokens") or 0),
        "rate_limited_count": int((totals or {}).get("rate_limited_count") or 0),
        "empty_count": int((totals or {}).get("empty_count") or 0),
        "by_provider": by_provider,
        "by_model": by_model,
        "by_tier": by_tier,
        "by_agent": by_agent,
        "by_project": by_project,
        "by_stream": by_stream,
    }


async def get_cache_summary(
    pool: asyncpg.Pool,
    *,
    hours: int,
    model_filter: str | None = None,
) -> dict:
    """Prompt-cache accounting over the window: totals + per-model breakdown.

    hit_rate = cache_read / (cache_read + cache_write + prompt)  — share of input
    served from cache. saved_usd = naive cost (everything billed at the input
    rate) minus actual cost (cache tiers applied), i.e. what caching saved.
    """
    conds = ["d.ts > NOW() - make_interval(hours => $1)", "o.decision_id IS NOT NULL"]
    params: list = [hours]
    if model_filter and model_filter != "*":
        params.append(model_filter)
        conds.append(f"d.decision_model = ${len(params)}")
    where = " AND ".join(conds)

    # Naive cost ≈ pretend every input token (fresh + read + write) was billed at
    # the model's fresh input rate; we approximate the rate from actual rows via
    # cost_usd, so we compute saved at the app layer instead. Here we surface the
    # token volumes + actual cost; the route computes saved against pricing.
    select_cols = """
        SUM(COALESCE(o.prompt_tokens, 0))::BIGINT      AS fresh_tokens,
        SUM(COALESCE(o.cache_read_tokens, 0))::BIGINT  AS cache_read_tokens,
        SUM(COALESCE(o.cache_write_tokens, 0))::BIGINT AS cache_write_tokens,
        SUM(COALESCE(o.completion_tokens, 0))::BIGINT  AS completion_tokens,
        SUM(o.cost_usd)::FLOAT                          AS actual_cost_usd,
        SUM(o.notional_cost_usd)::FLOAT                 AS notional_cost_usd,
        COUNT(*)                                        AS calls
    """

    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            f"""
            SELECT {select_cols}
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE {where}
            """,
            *params,
        )
        rows = await conn.fetch(
            f"""
            SELECT d.decision_model AS model, {select_cols}
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE {where}
             GROUP BY d.decision_model
             ORDER BY SUM(COALESCE(o.cache_read_tokens, 0)) DESC NULLS LAST
            """,
            *params,
        )

    def _shape(r) -> dict:
        fresh = int(r["fresh_tokens"] or 0)
        read = int(r["cache_read_tokens"] or 0)
        write = int(r["cache_write_tokens"] or 0)
        denom = fresh + read + write
        return {
            "fresh_tokens": fresh,
            "cache_read_tokens": read,
            "cache_write_tokens": write,
            "completion_tokens": int(r["completion_tokens"] or 0),
            "calls": int(r["calls"] or 0),
            "actual_cost_usd": r["actual_cost_usd"],
            "notional_cost_usd": r["notional_cost_usd"],
            "hit_rate": (read / denom) if denom else None,
            "write_read_ratio": (write / read) if read else None,
        }

    return {
        "window_hours": hours,
        "model_filter": model_filter or "*",
        "totals": _shape(totals) if totals else _shape({}),
        "by_model": [{"model": r["model"], **_shape(r)} for r in rows],
    }


async def get_prefix_reuse(
    pool: asyncpg.Pool,
    *,
    hours: int,
    limit: int = 20,
) -> dict:
    """Group outcomes by cacheable-prefix hash to find reuse vs. silent breaks.

    Returns three lists:
      top_reused — prefixes with the most cache-read tokens (caching working).
      leaky      — prefixes that write to cache but rarely read it back
                   (reuse_ratio < 1): a timestamp/ID is busting the prefix, or
                   the TTL expires before the second call.
      latency    — TTFT (first_byte_ms) spread per prefix. This is the SPEED lens
                   and the one that works for LOCAL models (Ollama/vLLM), which
                   report no cache tokens at all: a repeated prefix with low,
                   stable TTFT = KV cache warm; a wide spread = cache going cold /
                   thrashing between calls. Spread = p90 − p50.
    """
    where = "d.ts > NOW() - make_interval(hours => $1) AND o.prefix_hash IS NOT NULL"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT o.prefix_hash AS prefix_hash,
                   MAX(d.decision_model) AS model,
                   COUNT(*)                                        AS calls,
                   SUM(COALESCE(o.cache_read_tokens, 0))::BIGINT   AS reads,
                   SUM(COALESCE(o.cache_write_tokens, 0))::BIGINT  AS writes,
                   COUNT(o.first_byte_ms)                          AS ttft_n,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.first_byte_ms)  AS ttft_p50,
                   PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY o.first_byte_ms)  AS ttft_p90,
                   MIN(o.first_byte_ms)                            AS ttft_min,
                   MAX(o.first_byte_ms)                            AS ttft_max,
                   MAX(d.ts)                                       AS last_seen
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE {where}
             GROUP BY o.prefix_hash
            """,
            hours,
        )

    items = []
    for r in rows:
        reads = int(r["reads"] or 0)
        writes = int(r["writes"] or 0)
        p50 = float(r["ttft_p50"]) if r["ttft_p50"] is not None else None
        p90 = float(r["ttft_p90"]) if r["ttft_p90"] is not None else None
        items.append(
            {
                "prefix_hash": r["prefix_hash"],
                "model": r["model"],
                "calls": int(r["calls"] or 0),
                "reads": reads,
                "writes": writes,
                "reuse_ratio": (reads / writes) if writes else None,
                "ttft_n": int(r["ttft_n"] or 0),
                "ttft_p50_ms": round(p50) if p50 is not None else None,
                "ttft_spread_ms": (
                    round(p90 - p50) if p50 is not None and p90 is not None else None
                ),
                "ttft_min_ms": r["ttft_min"],
                "ttft_max_ms": r["ttft_max"],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
        )

    top_reused = sorted(items, key=lambda x: x["reads"], reverse=True)[:limit]
    # Leaky: wrote to cache but got little back. Needs ≥2 calls (a one-off write
    # that never recurs isn't a leak) and a poor reuse ratio.
    leaky = sorted(
        [
            x
            for x in items
            if x["writes"] > 0
            and x["calls"] >= 2
            and (x["reuse_ratio"] is None or x["reuse_ratio"] < 1.0)
        ],
        key=lambda x: x["writes"],
        reverse=True,
    )[:limit]
    # Latency: needs ≥2 timed calls on the same prefix. Sorted by spread desc so
    # the coldest/thrashing caches surface first (works for local + cloud).
    latency = sorted(
        [x for x in items if x["ttft_n"] >= 2 and x["ttft_spread_ms"] is not None],
        key=lambda x: x["ttft_spread_ms"],
        reverse=True,
    )[:limit]

    return {"window_hours": hours, "top_reused": top_reused, "leaky": leaky, "latency": latency}


async def get_cost_timeseries(
    pool: asyncpg.Pool,
    *,
    agent_id: str | None,
    bucket: str,
    hours: int,
    project_id: str | None = None,
) -> dict:
    """Bucketed cost series. ``agent_id=None`` / ``"*"`` returns aggregate
    across all agents; any other value filters. ``project_id`` further narrows.
    """
    bucket = bucket if bucket in ("hour", "day") else "hour"
    is_all = agent_id is None or agent_id == "*"
    conds: list[str] = ["d.ts > NOW() - make_interval(hours => $1)"]
    params: list = [hours]
    if not is_all:
        params.append(agent_id)
        conds.append(f"d.agent_id = ${len(params)}")
    if project_id and project_id != "*":
        params.append(project_id)
        conds.append(f"d.project_id = ${len(params)}")
    where = " AND ".join(conds)
    rows = await pool.fetch(
        f"""
        SELECT date_trunc('{bucket}', d.ts) AS bucket_ts,
               d.decision_provider          AS provider,
               SUM(o.cost_usd)::FLOAT       AS cost_usd,
               COUNT(*)                      AS calls
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE {where}
         GROUP BY bucket_ts, provider
         ORDER BY bucket_ts ASC
        """,
        *params,
    )

    series_map: dict[str, list[dict]] = {}
    for r in rows:
        provider = r["provider"] or "unknown"
        series_map.setdefault(provider, []).append(
            {
                "ts": r["bucket_ts"].isoformat() if r["bucket_ts"] else None,
                "cost_usd": r["cost_usd"],
                "calls": int(r["calls"]),
            }
        )

    return {
        "agent_id": "*" if is_all else agent_id,
        "bucket": bucket,
        "window_hours": hours,
        "series": [{"provider": p, "points": points} for p, points in series_map.items()],
    }


async def get_projects_with_stats(pool: asyncpg.Pool) -> list[dict]:
    """List projects (distinct project_id values) with their key/agent counts
    and 30-day activity. Drives the Cost tab's project dropdown.

    A "project" is a free-form text label on the api_keys row — there's no
    separate projects table; the label IS the identity.
    """
    rows = await pool.fetch(
        """
        SELECT k.project_id                                          AS project_id,
               COUNT(DISTINCT k.id)                                  AS key_count,
               COUNT(DISTINCT k.agent_id)                            AS agent_count,
               ARRAY_AGG(DISTINCT k.agent_id ORDER BY k.agent_id)    AS agents,
               COALESCE(c.call_count, 0)::BIGINT                     AS call_count_30d,
               c.total_cost_usd::FLOAT                               AS total_cost_usd_30d,
               c.last_call
          FROM nautgate.api_keys k
          LEFT JOIN (
              SELECT project_id,
                     COUNT(*)                         AS call_count,
                     SUM(o.cost_usd)                  AS total_cost_usd,
                     MAX(d.ts)                        AS last_call
                FROM nautgate.route_decisions d
                LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
               WHERE d.ts > NOW() - INTERVAL '30 days'
                 AND d.project_id IS NOT NULL
               GROUP BY project_id
          ) c ON c.project_id = k.project_id
         WHERE k.project_id IS NOT NULL
         GROUP BY k.project_id, c.call_count, c.total_cost_usd, c.last_call
         ORDER BY call_count_30d DESC, k.project_id ASC
        """
    )
    return [
        {
            "project_id": r["project_id"],
            "key_count": int(r["key_count"]),
            "agent_count": int(r["agent_count"]),
            "agents": list(r["agents"] or []),
            "call_count_30d": int(r["call_count_30d"] or 0),
            "total_cost_usd_30d": float(r["total_cost_usd_30d"] or 0)
            if r["total_cost_usd_30d"]
            else 0.0,
            "last_call": r["last_call"].isoformat() if r["last_call"] else None,
        }
        for r in rows
    ]


async def get_agents_with_key_counts(pool: asyncpg.Pool) -> list[dict]:
    """List distinct agent_ids from api_keys + how many keys each has + their
    cumulative activity (last 30 days). Used by the Cost tab's dropdown.
    """
    rows = await pool.fetch(
        """
        SELECT k.agent_id,
               COUNT(DISTINCT k.id)               AS key_count,
               COALESCE(c.call_count, 0)::BIGINT  AS call_count_30d,
               c.last_call
          FROM nautgate.api_keys k
          LEFT JOIN (
              SELECT agent_id,
                     COUNT(*)        AS call_count,
                     MAX(ts)         AS last_call
                FROM nautgate.route_decisions
               WHERE ts > NOW() - INTERVAL '30 days'
               GROUP BY agent_id
          ) c ON c.agent_id = k.agent_id
         GROUP BY k.agent_id, c.call_count, c.last_call
         ORDER BY call_count_30d DESC, k.agent_id ASC
        """
    )
    return [
        {
            "agent_id": r["agent_id"],
            "key_count": int(r["key_count"]),
            "call_count_30d": int(r["call_count_30d"] or 0),
            "last_call": r["last_call"].isoformat() if r["last_call"] else None,
        }
        for r in rows
    ]


async def get_decisions_for_findings_scan(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    hours: int,
    limit: int,
) -> list[dict]:
    """Pull recent rows for the Lighthouse-style privacy audit.

    Returns prompt_body when capture policy permitted (sensitivity != secret),
    plus the stored classified_signals JSONB for rows that had body suppressed.
    Also pulls ts so the audit can compute "last seen" per finding type.
    """
    rows = await pool.fetch(
        """
        SELECT d.id::text                AS decision_id,
               d.ts                       AS ts,
               d.agent_id                 AS agent_id,
               d.classified_sensitivity   AS classified_sensitivity,
               d.classified_signals       AS classified_signals,
               d.decision_model           AS decision_model,
               d.prompt_body              AS prompt_body
          FROM nautgate.route_decisions d
         WHERE d.agent_id = $1
           AND d.ts > NOW() - make_interval(hours => $2)
         ORDER BY d.ts DESC
         LIMIT $3
        """,
        agent_id,
        hours,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        if d.get("ts"):
            d["ts"] = d["ts"].isoformat()
        signals = d.get("classified_signals")
        if isinstance(signals, str):
            try:
                d["classified_signals"] = json.loads(signals)
            except (ValueError, TypeError):
                d["classified_signals"] = None
        out.append(d)
    return out


async def get_decision_detail(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    decision_id: str,
) -> dict | None:
    """Full row from route_decisions + matching outcome for one decision.

    Scoped to ``agent_id`` so an authenticated agent can only see its own
    decisions. Returns None if the decision_id doesn't exist or belongs to
    another agent.
    """
    # Local import — keep audit_meta light for tests that don't need it.
    from app.audit_meta import _content_text, _estimate_tokens  # noqa: PLC0415

    row = await pool.fetchrow(
        """
        SELECT d.id::text                     AS decision_id,
               d.ts                            AS ts,
               d.agent_id                      AS agent_id,
               d.inbound_format                AS inbound_format,
               d.model_requested               AS model_requested,
               d.classified_tier               AS classified_tier,
               d.classified_score              AS classified_score,
               d.classified_sensitivity        AS classified_sensitivity,
               d.classified_signals            AS classified_signals,
               d.brain_hints                   AS brain_hints,
               d.decision_provider             AS decision_provider,
               d.decision_model                AS decision_model,
               d.decision_reason               AS decision_reason,
               d.fallback_chain                AS fallback_chain,
               d.bloat_findings                AS bloat_findings,
               d.bloat_score                   AS bloat_score,
               d.estimated_waste_usd           AS estimated_waste_usd,
               d.prompt_excerpt                AS prompt_excerpt,
               d.prompt_body                   AS prompt_body,
               d.prompt_body_truncated_at_byte AS prompt_body_truncated_at_byte,
               d.tools_body                    AS tools_body,
               d.tools_body_truncated_at_byte  AS tools_body_truncated_at_byte,
               d.source_ip::text               AS source_ip,
               d.source_hostname               AS source_hostname,
               d.messages_count                AS messages_count,
               d.tools_count                   AS tools_count,
               d.stream_flag                   AS stream_flag,
               d.request_size_bytes            AS request_size_bytes,
               o.status_code                   AS status_code,
               o.duration_ms                   AS duration_ms,
               o.first_byte_ms                 AS first_byte_ms,
               o.prompt_tokens                 AS prompt_tokens,
               o.completion_tokens             AS completion_tokens,
               o.reasoning_tokens              AS reasoning_tokens,
               o.cost_usd                      AS cost_usd,
               o.was_empty                     AS was_empty,
               o.was_truncated                 AS was_truncated,
               o.client_disconnected           AS client_disconnected,
               o.response_body                 AS response_body,
               o.response_body_truncated_at_byte AS response_body_truncated_at_byte,
               o.response_size_bytes           AS response_size_bytes,
               o.tool_calls_made               AS tool_calls_made,
               o.actual_model                  AS actual_model,
               o.actual_provider               AS actual_provider,
               o.used_fallback                 AS used_fallback,
               o.fallback_count                AS fallback_count
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE d.id::text = $1 AND d.agent_id = $2
        """,
        decision_id,
        agent_id,
    )
    if row is None:
        return None
    d = dict(row)
    if d.get("ts"):
        d["ts"] = d["ts"].isoformat()
    if d.get("classified_score") is not None:
        d["classified_score"] = float(d["classified_score"])
    if d.get("cost_usd") is not None:
        d["cost_usd"] = float(d["cost_usd"])
    # JSONB fields come back as strings from asyncpg without a codec — try to parse.
    for k in (
        "classified_signals",
        "brain_hints",
        "tool_calls_made",
        "fallback_chain",
        "bloat_findings",
    ):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except (ValueError, TypeError):
                pass
    if d.get("bloat_score") is not None:
        d["bloat_score"] = float(d["bloat_score"])
    if d.get("estimated_waste_usd") is not None:
        d["estimated_waste_usd"] = float(d["estimated_waste_usd"])
    # Token breakdown computed on read from prompt_body when body was captured.
    d["token_estimate"] = _token_breakdown_from_body(
        d.get("prompt_body"), _content_text, _estimate_tokens
    )
    # Full payload anatomy — bytes/tokens per section + the raw content of each.
    # This is what answers "when I type 4 words, what *actually* ships upstream?"
    d["payload_anatomy"] = _payload_anatomy(
        d.get("prompt_body"), d.get("tools_body"), _content_text, _estimate_tokens
    )
    return d


def _payload_anatomy(
    prompt_body: str | None,
    tools_body: str | None,
    content_fn,
    est_fn,
) -> dict:
    """Break the captured request body into the categories the provider sees.

    Returns a dict with five keys: ``system``, ``tools``, ``history``, ``user``,
    ``totals``. Each section carries:
        bytes:    UTF-8 byte length of the serialized JSON for that section
        tokens:   rough char/4 token estimate
        items:    list of per-item dicts (so the UI can render expandable blocks)

    Sections may be empty (``items: []``) but the keys are always present so
    the frontend can render a stable layout.
    """

    def _utf8_len(text: str) -> int:
        return len(text.encode("utf-8"))

    def _section_bytes(items: list[dict]) -> int:
        if not items:
            return 0
        return _utf8_len(json.dumps(items, ensure_ascii=False, separators=(",", ":")))

    system_items: list[dict] = []
    history_items: list[dict] = []
    user_items: list[dict] = []
    tool_items: list[dict] = []

    # --- Parse prompt_body ---------------------------------------------
    messages: list[dict] | None = None
    if prompt_body:
        try:
            data = json.loads(prompt_body)
            if isinstance(data, list):
                messages = data
            elif isinstance(data, dict):
                m = data.get("messages")
                if isinstance(m, list):
                    messages = m
        except (ValueError, TypeError):
            messages = None

    if messages:
        last_user_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_idx = i
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            text = content_fn(m.get("content"))
            entry = {
                "role": role,
                "content": text,
                "bytes": _utf8_len(text),
                "tokens": est_fn(text),
            }
            if role == "system":
                system_items.append(entry)
            elif i == last_user_idx and role == "user":
                user_items.append(entry)
            else:
                history_items.append(entry)

    # --- Parse tools_body ----------------------------------------------
    if tools_body:
        try:
            tools = json.loads(tools_body)
        except (ValueError, TypeError):
            tools = None
        if isinstance(tools, list):
            for t in tools:
                if not isinstance(t, dict):
                    continue
                # OpenAI shape: {"type":"function","function":{"name":..., "description":..., "parameters":...}}
                # Anthropic shape: {"name":..., "description":..., "input_schema":...}
                fn = t.get("function") if isinstance(t.get("function"), dict) else t
                name = fn.get("name") or t.get("name") or "(unnamed)"
                desc = fn.get("description") or t.get("description") or ""
                schema = fn.get("parameters") or t.get("input_schema") or {}
                serialized = json.dumps(t, ensure_ascii=False, separators=(",", ":"))
                tool_items.append(
                    {
                        "name": name,
                        "description": desc,
                        "schema": schema,
                        "bytes": _utf8_len(serialized),
                        "tokens": est_fn(serialized),
                    }
                )

    def _section(items: list[dict]) -> dict:
        return {
            "bytes": _section_bytes(items),
            "tokens": sum(i.get("tokens", 0) for i in items),
            "count": len(items),
            "items": items,
        }

    sys_section = _section(system_items)
    tools_section = _section(tool_items)
    history_section = _section(history_items)
    user_section = _section(user_items)

    total_bytes = (
        sys_section["bytes"]
        + tools_section["bytes"]
        + history_section["bytes"]
        + user_section["bytes"]
    )
    total_tokens = (
        sys_section["tokens"]
        + tools_section["tokens"]
        + history_section["tokens"]
        + user_section["tokens"]
    )

    return {
        "system": sys_section,
        "tools": tools_section,
        "history": history_section,
        "user": user_section,
        "totals": {
            "bytes": total_bytes,
            "tokens": total_tokens,
            "user_pct": (user_section["bytes"] / total_bytes) if total_bytes else 0.0,
        },
    }


def _token_breakdown_from_body(body: str | None, content_fn, est_fn) -> dict | None:
    """Parse a JSON-serialized messages array into a system/tools/history/user
    token estimate. Returns None when body was suppressed by capture policy.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    # Body may be just messages list (the shape capture_prompt stores) — handle both.
    messages = (
        data if isinstance(data, list) else data.get("messages") if isinstance(data, dict) else None
    )
    if not isinstance(messages, list):
        return None
    last_user = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = i
    sys_text: list[str] = []
    hist_text: list[str] = []
    user_text = ""
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = content_fn(m.get("content"))
        if role == "system":
            sys_text.append(text)
        elif i == last_user and role == "user":
            user_text = text
        else:
            hist_text.append(text)
    return {
        "system": est_fn("\n".join(sys_text)),
        "tools": 0,  # tools aren't in prompt_body (they're top-level on the request); show 0 when reading from body alone
        "history": est_fn("\n".join(hist_text)),
        "user": est_fn(user_text),
    }


async def get_discovered_agents(
    pool: asyncpg.Pool,
    *,
    hours: int = 168,
) -> list[dict]:
    """Distinct agent_ids that have produced traffic in the last <hours>.

    Used by the dashboard for auto-discovery so OAuth-derived sessions
    (claude-oauth-…, codex-…) appear in the session picker without the
    operator having to mint and paste an ng_ token.
    """
    rows = await pool.fetch(
        """
        SELECT d.agent_id,
               MAX(d.ts)   AS last_seen_at,
               COUNT(*)    AS request_count
          FROM nautgate.route_decisions d
         WHERE d.ts > now() - ($1 || ' hours')::interval
           AND d.agent_id IS NOT NULL
         GROUP BY d.agent_id
         ORDER BY last_seen_at DESC
        """,
        str(hours),
    )
    out = []
    for r in rows:
        d = dict(r)
        if d.get("last_seen_at"):
            d["last_seen_at"] = d["last_seen_at"].isoformat()
        out.append(d)
    return out


async def get_recent_decisions(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    limit: int,
) -> list[dict]:
    """Last N route_decisions for the agent, joined with their outcome row.

    Returns one dict per decision, ts-DESC ordered. Outcome fields are NULL
    when the request errored before persist_outcome ran (rare).
    """
    rows = await pool.fetch(
        """
        SELECT d.id::text          AS decision_id,
               d.ts                 AS ts,
               d.inbound_format     AS inbound_format,
               d.model_requested    AS model_requested,
               d.classified_tier    AS tier,
               d.classified_score   AS score,
               d.classified_sensitivity AS sensitivity,
               d.decision_provider  AS provider,
               d.decision_model     AS model,
               d.decision_reason    AS reason,
               d.source_hostname    AS source_hostname,
               d.source_ip::text    AS source_ip,
               d.messages_count     AS messages_count,
               d.tools_count        AS tools_count,
               d.stream_flag        AS stream_flag,
               d.request_size_bytes AS request_size_bytes,
               d.bloat_score        AS bloat_score,
               d.estimated_waste_usd AS estimated_waste_usd,
               o.status_code        AS status_code,
               o.duration_ms        AS duration_ms,
               o.first_byte_ms      AS first_byte_ms,
               o.prompt_tokens      AS prompt_tokens,
               o.completion_tokens  AS completion_tokens,
               o.reasoning_tokens   AS reasoning_tokens,
               o.used_fallback      AS used_fallback,
               o.fallback_count     AS fallback_count,
               o.cost_usd           AS cost_usd,
               o.was_empty          AS was_empty,
               o.was_truncated      AS was_truncated,
               o.client_disconnected AS client_disconnected,
               o.response_size_bytes AS response_size_bytes,
               o.tool_calls_made    AS tool_calls_made,
               o.actual_model       AS actual_model,
               o.actual_provider    AS actual_provider
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE d.agent_id = $1
         ORDER BY d.ts DESC
         LIMIT $2
        """,
        agent_id,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        if d.get("ts"):
            d["ts"] = d["ts"].isoformat()
        if d.get("score") is not None:
            d["score"] = float(d["score"])
        if d.get("cost_usd") is not None:
            d["cost_usd"] = float(d["cost_usd"])
        if d.get("bloat_score") is not None:
            d["bloat_score"] = float(d["bloat_score"])
        if d.get("estimated_waste_usd") is not None:
            d["estimated_waste_usd"] = float(d["estimated_waste_usd"])
        if isinstance(d.get("tool_calls_made"), str):
            try:
                d["tool_calls_made"] = json.loads(d["tool_calls_made"])
            except (ValueError, TypeError):
                d["tool_calls_made"] = None
        out.append(d)
    return out


async def get_stats(pool: asyncpg.Pool, *, agent_id: str, hours: int) -> dict:
    """Aggregate stats over recent route_decisions + route_outcomes for one agent.

    Returns the dict shape expected by /v1/stats. All counts default to 0,
    rates to 0.0, cost_usd_total to None when there are no rows.
    """
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                 AS requests_total,
                COUNT(*) FILTER (WHERE o.was_empty)                      AS empty_count,
                AVG(o.duration_ms)::FLOAT                                AS avg_latency_ms,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.duration_ms)
                                                                          AS p50_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY o.duration_ms)
                                                                          AS p95_ms,
                SUM(o.cost_usd)::FLOAT                                   AS cost_usd_total
            FROM nautgate.route_decisions d
            LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
            WHERE d.agent_id = $1
              AND d.ts > NOW() - make_interval(hours => $2)
            """,
            agent_id,
            hours,
        )
        by_tier = await conn.fetch(
            """
            SELECT classified_tier AS k, COUNT(*) AS n
              FROM nautgate.route_decisions
             WHERE agent_id = $1 AND ts > NOW() - make_interval(hours => $2)
             GROUP BY classified_tier
             ORDER BY n DESC
            """,
            agent_id,
            hours,
        )
        by_format = await conn.fetch(
            """
            SELECT inbound_format AS k, COUNT(*) AS n
              FROM nautgate.route_decisions
             WHERE agent_id = $1 AND ts > NOW() - make_interval(hours => $2)
             GROUP BY inbound_format
             ORDER BY n DESC
            """,
            agent_id,
            hours,
        )

    requests_total = (totals or {}).get("requests_total") or 0
    empty_count = (totals or {}).get("empty_count") or 0
    return {
        "agent_id": agent_id,
        "window_hours": hours,
        "requests_total": int(requests_total),
        "empty_count": int(empty_count),
        "empty_rate": (empty_count / requests_total) if requests_total else 0.0,
        "latency_ms": {
            "avg": (totals or {}).get("avg_latency_ms"),
            "p50": (totals or {}).get("p50_ms"),
            "p95": (totals or {}).get("p95_ms"),
        },
        "cost_usd_total": (totals or {}).get("cost_usd_total"),
        "requests_by_tier": {r["k"]: int(r["n"]) for r in by_tier},
        "requests_by_inbound_format": {r["k"]: int(r["n"]) for r in by_format},
    }


# ── Quality eval (LLM-as-judge over the audit log) ─────────────────────────
# Backs the Quality tab and the Coach accordion in the Audit drawer. Inserts
# are fire-and-forget from the post-outcome hook; reads serve the dashboard.


async def insert_quality_eval(
    pool: asyncpg.Pool,
    *,
    decision_id: UUID | str,
    judge_provider: str,
    judge_model: str,
    judge_cost_usd: float | None,
    judge_latency_ms: int | None,
    rubric: dict | None,
    failure_tags: list[str] | None,
    suggested_prompt: str | None,
    coach_notes: str | None,
    trigger: str,
    anti_pattern: str | None = None,
) -> None:
    rubric_json = json.dumps(rubric) if rubric is not None else None
    tags = list(failure_tags or [])
    did = decision_id if isinstance(decision_id, UUID) else UUID(str(decision_id))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.quality_evals
                (decision_id, judge_provider, judge_model, judge_cost_usd,
                 judge_latency_ms, rubric, failure_tags, suggested_prompt,
                 coach_notes, trigger, anti_pattern)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
            ON CONFLICT (decision_id) DO UPDATE SET
                ts = now(),
                judge_provider = EXCLUDED.judge_provider,
                judge_model = EXCLUDED.judge_model,
                judge_cost_usd = EXCLUDED.judge_cost_usd,
                judge_latency_ms = EXCLUDED.judge_latency_ms,
                rubric = EXCLUDED.rubric,
                failure_tags = EXCLUDED.failure_tags,
                suggested_prompt = EXCLUDED.suggested_prompt,
                coach_notes = EXCLUDED.coach_notes,
                trigger = EXCLUDED.trigger,
                anti_pattern = EXCLUDED.anti_pattern
            """,
            did,
            judge_provider,
            judge_model,
            judge_cost_usd,
            judge_latency_ms,
            rubric_json,
            tags,
            suggested_prompt,
            coach_notes,
            trigger,
            anti_pattern,
        )


async def get_quality_eval(pool: asyncpg.Pool, decision_id: str | UUID) -> dict | None:
    did = decision_id if isinstance(decision_id, UUID) else UUID(str(decision_id))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT q.decision_id, q.ts, q.judge_provider, q.judge_model,
                   q.judge_cost_usd, q.judge_latency_ms, q.rubric, q.failure_tags,
                   q.suggested_prompt, q.coach_notes, q.trigger, q.user_feedback,
                   d.decision_model, d.decision_provider, d.classified_tier,
                   d.classified_score, d.prompt_excerpt
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE q.decision_id = $1
            """,
            did,
        )
    if row is None:
        return None
    out = dict(row)
    out["decision_id"] = str(out["decision_id"])
    if isinstance(out.get("rubric"), str):
        try:
            out["rubric"] = json.loads(out["rubric"])
        except (ValueError, TypeError):
            pass
    if out.get("judge_cost_usd") is not None:
        out["judge_cost_usd"] = float(out["judge_cost_usd"])
    if out.get("classified_score") is not None:
        out["classified_score"] = float(out["classified_score"])
    return out


async def get_daily_judge_spend(pool: asyncpg.Pool) -> float:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(judge_cost_usd), 0)::FLOAT AS spend "
            "FROM nautgate.quality_evals WHERE date(ts) = current_date"
        )
    return float((row or {}).get("spend") or 0.0)


# Known failure tags surfaced in the UI heatmap. Keep the set fixed so the
# table layout stays stable; new tags emitted by the judge that aren't in
# this list still get stored, they just don't get their own column.
QUALITY_FAILURE_TAGS = [
    "over_thinking",
    "off_task",
    "looped",
    "hallucination",
    "partial_answer",
    "refusal",
    "tool_misuse",
]


async def get_behavior_per_model(
    pool: asyncpg.Pool,
    *,
    hours: int = 168,
) -> list[dict]:
    """Per-model behavioral analytics for the Behavior tab.

    For each model with quality_evals in the window, returns:
      - evals: how many quality evals
      - avg_action_compliance: mean rubric.action_compliance (0-5)
      - avg_task_completion: mean rubric.task_completion (0-5)
      - avg_reasoning_efficiency
      - avg_reasoning_tokens: from route_outcomes
      - avg_duration_ms
      - skipped_doc_rate / edit_without_read_rate / premature_action_rate
        / retry_loop_rate: fraction of evals carrying that tag

    Backbone of the cowboy comparison. NULL action_compliance values
    fall out of the average — old evals from before the rubric change
    don't poison newer scores.
    """
    rows = await pool.fetch(
        """
        SELECT d.model_requested AS model,
               COUNT(*)                                                   AS evals,
               AVG((q.rubric->>'action_compliance')::numeric)             AS avg_action_compliance,
               AVG((q.rubric->>'task_completion')::numeric)               AS avg_task_completion,
               AVG((q.rubric->>'reasoning_efficiency')::numeric)          AS avg_reasoning_efficiency,
               AVG(o.reasoning_tokens)::numeric                           AS avg_reasoning_tokens,
               AVG(o.duration_ms)::numeric                                AS avg_duration_ms,
               AVG(CASE WHEN 'skipped_doc'        = ANY(q.failure_tags) THEN 1.0 ELSE 0.0 END) AS skipped_doc_rate,
               AVG(CASE WHEN 'edit_without_read'  = ANY(q.failure_tags) THEN 1.0 ELSE 0.0 END) AS edit_without_read_rate,
               AVG(CASE WHEN 'premature_action'   = ANY(q.failure_tags) THEN 1.0 ELSE 0.0 END) AS premature_action_rate,
               AVG(CASE WHEN 'retry_loop'         = ANY(q.failure_tags) THEN 1.0 ELSE 0.0 END) AS retry_loop_rate
          FROM nautgate.quality_evals q
          JOIN nautgate.route_decisions d ON d.id = q.decision_id
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = q.decision_id
         WHERE q.ts > NOW() - make_interval(hours => $1)
         GROUP BY d.model_requested
        HAVING COUNT(*) >= 1
         ORDER BY evals DESC
        """,
        hours,
    )
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for k in (
            "avg_action_compliance",
            "avg_task_completion",
            "avg_reasoning_efficiency",
            "avg_reasoning_tokens",
            "avg_duration_ms",
            "skipped_doc_rate",
            "edit_without_read_rate",
            "premature_action_rate",
            "retry_loop_rate",
        ):
            if d.get(k) is not None:
                d[k] = float(d[k])
        out.append(d)
    return out


async def get_behavior_trace(
    pool: asyncpg.Pool,
    *,
    decision_id: str | UUID,
) -> dict | None:
    """Full prompt-action trace for a single decision.

    Returns:
      decision_id, ts, model, agent_id,
      prompt_body (the user's last message — for verb/target extraction
                   on the frontend), tool_calls_made (the raw JSONB
                   sequence), reasoning_tokens, duration_ms, first_byte_ms,
      quality_eval (the rubric scores + tags + coach_notes, or None).
    """
    did = decision_id if isinstance(decision_id, UUID) else UUID(str(decision_id))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id::text                AS decision_id,
                   d.ts                      AS ts,
                   d.agent_id                AS agent_id,
                   d.model_requested         AS model,
                   d.decision_provider       AS provider,
                   d.prompt_body             AS prompt_body,
                   d.tools_count             AS tools_count,
                   o.tool_calls_made         AS tool_calls_made,
                   o.reasoning_tokens        AS reasoning_tokens,
                   o.duration_ms             AS duration_ms,
                   o.first_byte_ms           AS first_byte_ms,
                   o.status_code             AS status_code,
                   q.rubric                  AS rubric,
                   q.failure_tags            AS failure_tags,
                   q.coach_notes             AS coach_notes,
                   q.suggested_prompt        AS suggested_prompt
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
              LEFT JOIN nautgate.quality_evals   q ON q.decision_id = d.id
             WHERE d.id = $1
            """,
            did,
        )
    if row is None:
        return None
    d = dict(row)
    if d.get("ts"):
        d["ts"] = d["ts"].isoformat()
    if isinstance(d.get("tool_calls_made"), str):
        try:
            d["tool_calls_made"] = json.loads(d["tool_calls_made"])
        except (ValueError, TypeError):
            d["tool_calls_made"] = None
    if isinstance(d.get("rubric"), str):
        try:
            d["rubric"] = json.loads(d["rubric"])
        except (ValueError, TypeError):
            d["rubric"] = None
    return d


async def get_quality_summary(
    pool: asyncpg.Pool,
    *,
    hours: int,
    model_filter: str | None = None,
) -> dict:
    """Aggregate quality_evals for the Quality tab.

    Returns:
        totals: {evaluations, avg_task_completion, failure_rate, judge_spend_usd}
        by_model: [{model, evaluations, avg_completion, failure_rate}, …]
        failure_modes: [{model, <tag>: count, …}, …]
        heatmap: [{model, buckets: {0_2, 2_4, 4_6, 6_8, 8_10}, …}, …]
            bucket value is the failure rate (0.0-1.0) inside that
            classified_score bucket for that model.
        worst_recent: [{decision_id, ts, model, tier, completion, tags, note}]
    """
    conds: list[str] = ["q.ts > NOW() - make_interval(hours => $1)"]
    params: list = [hours]
    if model_filter and model_filter != "*":
        params.append(model_filter)
        conds.append(f"d.decision_model = ${len(params)}")
    where = " AND ".join(conds)

    # A "failure" is any failure_tag present OR a task_completion score below 3.
    failure_expr = (
        "(coalesce(array_length(q.failure_tags, 1), 0) > 0 "
        "OR coalesce((q.rubric->>'task_completion')::numeric, 5) < 3)"
    )

    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS evaluations,
                   AVG((q.rubric->>'task_completion')::numeric)::FLOAT
                       AS avg_task_completion,
                   AVG(CASE WHEN {failure_expr} THEN 1 ELSE 0 END)::FLOAT
                       AS failure_rate,
                   COALESCE(SUM(q.judge_cost_usd), 0)::FLOAT AS judge_spend_usd
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE {where}
            """,
            *params,
        )

        by_model_rows = await conn.fetch(
            f"""
            SELECT d.decision_model AS model,
                   COUNT(*) AS evaluations,
                   AVG((q.rubric->>'task_completion')::numeric)::FLOAT AS avg_completion,
                   AVG(CASE WHEN {failure_expr} THEN 1 ELSE 0 END)::FLOAT AS failure_rate
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE {where}
             GROUP BY d.decision_model
             ORDER BY failure_rate DESC NULLS LAST, evaluations DESC
            """,
            *params,
        )

        # Failure-modes breakdown — one column per known tag.
        # Tag names come from a hard-coded Python enum, so string-inlining is
        # safe; no SQL injection surface.
        tag_cols = ", ".join(
            f"SUM(CASE WHEN '{t}' = ANY(q.failure_tags) THEN 1 ELSE 0 END) AS {t}"
            for t in QUALITY_FAILURE_TAGS
        )
        failure_modes_rows = await conn.fetch(
            f"""
            SELECT d.decision_model AS model,
                   COUNT(*) AS evaluations,
                   {tag_cols}
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE {where}
             GROUP BY d.decision_model
             ORDER BY evaluations DESC
            """,
            *params,
        )

        # Heatmap: per-model failure rate per classified_score bucket.
        heatmap_rows = await conn.fetch(
            f"""
            SELECT d.decision_model AS model,
                   width_bucket(coalesce(d.classified_score::numeric, 0), 0, 10, 5)
                       AS bucket_idx,
                   COUNT(*) AS evaluations,
                   AVG(CASE WHEN {failure_expr} THEN 1 ELSE 0 END)::FLOAT AS failure_rate
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE {where}
             GROUP BY d.decision_model, bucket_idx
            """,
            *params,
        )

        worst_rows = await conn.fetch(
            f"""
            SELECT q.decision_id, q.ts, d.decision_model AS model,
                   d.classified_tier AS tier,
                   (q.rubric->>'task_completion')::numeric AS completion,
                   q.failure_tags, q.coach_notes
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE {where} AND {failure_expr}
             ORDER BY (q.rubric->>'task_completion')::numeric ASC NULLS FIRST,
                      q.ts DESC
             LIMIT 25
            """,
            *params,
        )

    bucket_labels = ["0_2", "2_4", "4_6", "6_8", "8_10"]
    heatmap_by_model: dict[str, dict] = {}
    for r in heatmap_rows:
        m = r["model"]
        slot = heatmap_by_model.setdefault(
            m,
            {
                "model": m,
                "buckets": {b: None for b in bucket_labels},
                "counts": {b: 0 for b in bucket_labels},
            },
        )
        # width_bucket(value, 0, 10, 5) returns 1..5 for in-range, 0 for <0, 6 for >=10
        idx = max(1, min(5, int(r["bucket_idx"] or 1)))
        label = bucket_labels[idx - 1]
        slot["buckets"][label] = float(r["failure_rate"] or 0.0)
        slot["counts"][label] = int(r["evaluations"] or 0)

    return {
        "window_hours": hours,
        "totals": {
            "evaluations": int((totals or {}).get("evaluations") or 0),
            "avg_task_completion": (totals or {}).get("avg_task_completion"),
            "failure_rate": (totals or {}).get("failure_rate"),
            "judge_spend_usd": (totals or {}).get("judge_spend_usd"),
        },
        "by_model": [
            {
                "model": r["model"],
                "evaluations": int(r["evaluations"]),
                "avg_completion": float(r["avg_completion"])
                if r["avg_completion"] is not None
                else None,
                "failure_rate": float(r["failure_rate"]) if r["failure_rate"] is not None else None,
            }
            for r in by_model_rows
        ],
        "failure_modes": [
            {
                "model": r["model"],
                "evaluations": int(r["evaluations"]),
                **{t: int(r[t] or 0) for t in QUALITY_FAILURE_TAGS},
            }
            for r in failure_modes_rows
        ],
        "heatmap": list(heatmap_by_model.values()),
        "worst_recent": [
            {
                "decision_id": str(r["decision_id"]),
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "model": r["model"],
                "tier": r["tier"],
                "completion": float(r["completion"]) if r["completion"] is not None else None,
                "failure_tags": list(r["failure_tags"] or []),
                "coach_notes": r["coach_notes"],
            }
            for r in worst_rows
        ],
    }


def excerpt_last_user_message(messages: list[dict] | None, *, max_chars: int = 200) -> str | None:
    """Find the last user message and excerpt its text content. Returns None if not found."""
    if not messages:
        return None
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content[:max_chars]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        return text[:max_chars]
        return None
    return None


# ── LLM-Probing ─────────────────────────────────────────────────────────────


async def get_probe_config(pool: asyncpg.Pool) -> dict:
    row = await pool.fetchrow(
        """SELECT enabled, interval_hours, targets, last_run_at, next_run_at, updated_at
             FROM nautgate.llm_probe_config WHERE id = 1"""
    )
    if row is None:
        return {
            "enabled": False,
            "interval_hours": 6,
            "targets": [],
            "last_run_at": None,
            "next_run_at": None,
        }
    d = dict(row)
    d["targets"] = list(d.get("targets") or [])
    for k in ("last_run_at", "next_run_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


async def update_probe_config(
    pool: asyncpg.Pool,
    *,
    enabled: bool | None = None,
    interval_hours: int | None = None,
    targets: list[str] | None = None,
    last_run_at=None,
    next_run_at=None,
    touch_runs: bool = False,
) -> None:
    """Patch the singleton probe config. Only non-None fields are written.

    ``touch_runs=True`` writes last_run_at/next_run_at literally (used by the
    scheduler after a cycle); otherwise those are left untouched unless passed.
    """
    sets: list[str] = ["updated_at = now()"]
    params: list = []

    def _set(col: str, val) -> None:
        params.append(val)
        sets.append(f"{col} = ${len(params)}")

    if enabled is not None:
        _set("enabled", enabled)
    if interval_hours is not None:
        _set("interval_hours", interval_hours)
    if targets is not None:
        _set("targets", targets)
    if touch_runs or last_run_at is not None:
        _set("last_run_at", last_run_at)
    if touch_runs or next_run_at is not None:
        _set("next_run_at", next_run_at)
    await pool.execute(
        f"UPDATE nautgate.llm_probe_config SET {', '.join(sets)} WHERE id = 1", *params
    )


async def insert_probe_run(pool: asyncpg.Pool, **f) -> None:
    await pool.execute(
        """
        INSERT INTO nautgate.llm_probe_runs
            (cycle_id, probe_name, provider, model, via, observed_model,
             prompt_bytes, prompt_tokens, completion_tokens, tokens_per_byte,
             response_sha, response_text, first_byte_ms, duration_ms,
             status_code, quality_score, refused, cost_usd, error)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
        """,
        f["cycle_id"],
        f["probe_name"],
        f["provider"],
        f["model"],
        f["via"],
        f.get("observed_model"),
        f.get("prompt_bytes"),
        f.get("prompt_tokens"),
        f.get("completion_tokens"),
        f.get("tokens_per_byte"),
        f.get("response_sha"),
        f.get("response_text"),
        f.get("first_byte_ms"),
        f.get("duration_ms"),
        f.get("status_code"),
        f.get("quality_score"),
        bool(f.get("refused", False)),
        f.get("cost_usd"),
        f.get("error"),
    )


async def insert_probe_alert(pool: asyncpg.Pool, **f) -> None:
    await pool.execute(
        """
        INSERT INTO nautgate.llm_probe_alerts
            (cycle_id, provider, model, alert_type, severity, detail)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        """,
        f.get("cycle_id"),
        f["provider"],
        f["model"],
        f["alert_type"],
        f.get("severity", "warning"),
        json.dumps(f.get("detail") or {}),
    )


async def get_probe_alerts(pool: asyncpg.Pool, *, hours: int = 168) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id::text, ts, provider, model, alert_type, severity, detail, resolved_at
             FROM nautgate.llm_probe_alerts
            WHERE ts > NOW() - make_interval(hours => $1)
            ORDER BY ts DESC LIMIT 200""",
        hours,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
        d["resolved_at"] = d["resolved_at"].isoformat() if d["resolved_at"] else None
        out.append(d)
    return out


async def get_probe_baseline(pool, *, provider, via, model, metric) -> dict | None:
    row = await pool.fetchrow(
        """SELECT ewma_mean, ewma_variance, sample_count, consecutive_anomalies
             FROM nautgate.llm_probe_baselines
            WHERE provider=$1 AND via=$2 AND model=$3 AND metric=$4""",
        provider,
        via,
        model,
        metric,
    )
    return dict(row) if row else None


async def upsert_probe_baseline(
    pool,
    *,
    provider,
    via,
    model,
    metric,
    ewma_mean,
    ewma_variance,
    sample_count,
    consecutive_anomalies,
    last_observed,
    last_z_score,
) -> None:
    await pool.execute(
        """
        INSERT INTO nautgate.llm_probe_baselines
            (provider, via, model, metric, ewma_mean, ewma_variance, sample_count,
             consecutive_anomalies, last_observed, last_z_score, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now())
        ON CONFLICT (provider, via, model, metric) DO UPDATE SET
            ewma_mean=$5, ewma_variance=$6, sample_count=$7,
            consecutive_anomalies=$8, last_observed=$9, last_z_score=$10, updated_at=now()
        """,
        provider,
        via,
        model,
        metric,
        ewma_mean,
        ewma_variance,
        sample_count,
        consecutive_anomalies,
        last_observed,
        last_z_score,
    )


async def get_probe_summary(pool: asyncpg.Pool, *, hours: int = 168) -> dict:
    """Latest cycle's runs per (provider, model), split by transport leg, so the
    dashboard can show subscription vs metered side-by-side + provenance."""
    latest = await pool.fetchval(
        "SELECT cycle_id FROM nautgate.llm_probe_runs ORDER BY ts DESC LIMIT 1"
    )
    targets: dict = {}
    if latest is not None:
        rows = await pool.fetch(
            """SELECT probe_name, provider, model, via, observed_model, tokens_per_byte,
                      first_byte_ms, duration_ms, quality_score, refused, status_code, error
                 FROM nautgate.llm_probe_runs WHERE cycle_id = $1 ORDER BY model, via""",
            latest,
        )
        for r in rows:
            key = f"{r['provider']}/{r['model']}"
            t = targets.setdefault(
                key, {"provider": r["provider"], "model": r["model"], "legs": {}}
            )
            leg = t["legs"].setdefault(
                r["via"],
                {
                    "via": r["via"],
                    "observed_model": None,
                    "tokens_per_byte": None,
                    "first_byte_ms": None,
                    "quality_score": None,
                    "refused": False,
                    "status_code": r["status_code"],
                    "error": None,
                },
            )
            # Pull each fingerprint from its dedicated probe, not whichever row
            # sorted first (provenance_ping's tiny prompt has a different ratio).
            if leg["observed_model"] is None and r["observed_model"]:
                leg["observed_model"] = r["observed_model"]
            if r["probe_name"] == "tokenizer_fp" and r["tokens_per_byte"] is not None:
                leg["tokens_per_byte"] = float(r["tokens_per_byte"])
            if r["probe_name"] == "latency_ping" and r["first_byte_ms"] is not None:
                leg["first_byte_ms"] = r["first_byte_ms"]
            if r["probe_name"] == "quality_reason" and r["quality_score"] is not None:
                leg["quality_score"] = float(r["quality_score"])
            if r["refused"]:
                leg["refused"] = True
            if r["error"] and leg["error"] is None:
                leg["error"] = r["error"]
    return {
        "latest_cycle": str(latest) if latest else None,
        "targets": list(targets.values()),
        "alerts": await get_probe_alerts(pool, hours=hours),
    }


async def get_probe_history(pool: asyncpg.Pool, *, model: str, hours: int = 720) -> list[dict]:
    rows = await pool.fetch(
        """SELECT ts, via, probe_name, tokens_per_byte, first_byte_ms,
                  quality_score, observed_model, refused
             FROM nautgate.llm_probe_runs
            WHERE model = $1 AND ts > NOW() - make_interval(hours => $2)
            ORDER BY ts DESC LIMIT 500""",
        model,
        hours,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
        d["tokens_per_byte"] = (
            float(d["tokens_per_byte"]) if d["tokens_per_byte"] is not None else None
        )
        d["quality_score"] = float(d["quality_score"]) if d["quality_score"] is not None else None
        out.append(d)
    return out


async def get_provider_status(pool: asyncpg.Pool, *, minutes: int = 10) -> dict:
    """Per-provider liveness from the last N minutes of REAL traffic.

    Buckets outcomes into success (2xx) / overloaded (529 + retries absorbed) /
    rate_limited (429) / error (other >=400). status:
      down     — recent calls but no success and overload/error dominate
      degraded — some overload/429/error mixed with success
      up       — overwhelmingly 2xx
      no-data  — no calls in the window
    """
    rows = await pool.fetch(
        """
        SELECT d.decision_provider AS provider,
               COUNT(*)                                                   AS total,
               COUNT(*) FILTER (WHERE o.status_code BETWEEN 200 AND 299)  AS ok,
               COUNT(*) FILTER (WHERE o.status_code = 529)                AS overloaded,
               COUNT(*) FILTER (WHERE o.status_code = 429)                AS rate_limited,
               COUNT(*) FILTER (WHERE o.status_code >= 400 AND o.status_code NOT IN (429,529)) AS errors,
               COALESCE(SUM(o.upstream_overload_retries), 0)::INT         AS retries_absorbed,
               MAX(o.ts)                                                  AS last_seen
          FROM nautgate.route_outcomes o
          JOIN nautgate.route_decisions d ON d.id = o.decision_id
         WHERE o.ts > NOW() - make_interval(mins => $1)
         GROUP BY d.decision_provider
        """,
        minutes,
    )
    out = {}
    for r in rows:
        total = int(r["total"] or 0)
        ok = int(r["ok"] or 0)
        overloaded = int(r["overloaded"] or 0)
        retries = int(r["retries_absorbed"] or 0)
        rl = int(r["rate_limited"] or 0)
        errors = int(r["errors"] or 0)
        # True overload events include the ones a retry absorbed (final row was 2xx).
        overload_events = overloaded + retries
        denom = ok + overload_events + rl + errors
        overload_pct = (overload_events / denom) if denom else 0.0
        bad = overloaded + rl + errors
        if total == 0:
            status = "no-data"
        elif ok == 0 and bad > 0:
            status = "down"
        elif overload_pct >= 0.05 or rl > 0 or errors > 0:
            status = "degraded"
        else:
            status = "up"
        out[r["provider"]] = {
            "status": status,
            "total": total,
            "success": ok,
            "overloaded": overloaded,
            "rate_limited": rl,
            "errors": errors,
            "retries_absorbed": retries,
            "overload_pct": round(overload_pct, 4),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        }
    return out


# --- API key management (Settings → Keys: name + TTL + revoke) ----------
async def create_api_key(
    pool: asyncpg.Pool,
    *,
    name: str,
    agent_id: str,
    ttl_days: int | None,
    profile: str = "auto",
    override_model: str | None = None,
) -> dict:
    """Mint a key with a name + optional TTL. Returns metadata + the plaintext
    token (shown once, never stored). ``override_model`` pins the key to one
    model (NAUTGATE-3) — the model is then chosen by the key, not the client."""
    from app.auth import issue_key

    plaintext, key_id, key_hash = issue_key()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nautgate.api_keys
                (id, key_hash, agent_id, name, default_profile, override_model, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6,
                    CASE WHEN $7::int IS NULL THEN NULL ELSE NOW() + make_interval(days => $7) END)
            RETURNING id::text, name, agent_id, default_profile, override_model, created_at, expires_at
            """,
            key_id,
            key_hash,
            agent_id,
            name,
            profile,
            override_model or None,
            ttl_days,
        )
    d = dict(row)
    for k in ("created_at", "expires_at"):
        d[k] = d[k].isoformat() if d[k] else None
    d["token"] = plaintext
    return d


async def list_api_keys(pool: asyncpg.Pool) -> list[dict]:
    """All keys with status metadata (no secrets)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, name, agent_id, default_profile, override_model,
                   created_at, last_used_at, expires_at, revoked_at
            FROM nautgate.api_keys
            ORDER BY created_at DESC
            """
        )
    from datetime import datetime

    now = datetime.now(UTC)
    out = []
    for r in rows:
        d = dict(r)
        if d["revoked_at"] is not None:
            d["status"] = "revoked"
        elif d["expires_at"] is not None and d["expires_at"] < now:
            d["status"] = "expired"
        else:
            d["status"] = "active"
        for k in ("created_at", "last_used_at", "expires_at", "revoked_at"):
            d[k] = d[k].isoformat() if d[k] else None
        out.append(d)
    return out


async def revoke_api_key(pool: asyncpg.Pool, key_id: str) -> bool:
    """Soft-revoke a key. Returns True if a row was revoked."""
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE nautgate.api_keys SET revoked_at = NOW() WHERE id = $1::uuid AND revoked_at IS NULL",
            key_id,
        )
    return res.endswith("1")


# ── Provider credentials (NAUTGATE-8) ──────────────────────────────────────
# Encrypted-at-rest provider API keys. Plaintext only ever exists transiently
# (on set, and on the read path that forwards a request). Never logged, never
# returned by a list/read endpoint.

VALID_PROVIDERS = ("openrouter", "anthropic", "openai", "gemini")


async def set_provider_credential(pool: asyncpg.Pool, *, provider: str, plaintext: str) -> dict:
    """Encrypt a provider key and upsert it. Returns display metadata (no secret)."""
    from app import crypto

    if provider not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {VALID_PROVIDERS}")
    plaintext = plaintext.strip()
    if not plaintext:
        raise ValueError("empty key")
    ciphertext, nonce = crypto.encrypt(plaintext)
    row = await pool.fetchrow(
        """
        INSERT INTO nautgate.provider_credentials (provider, ciphertext, nonce, last4, updated_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (provider) DO UPDATE
            SET ciphertext = EXCLUDED.ciphertext,
                nonce      = EXCLUDED.nonce,
                last4      = EXCLUDED.last4,
                updated_at = NOW()
        RETURNING provider, last4, updated_at
        """,
        provider,
        ciphertext,
        nonce,
        crypto.last4(plaintext),
    )
    d = dict(row)
    d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
    return d


async def get_provider_credential(pool: asyncpg.Pool, provider: str) -> str | None:
    """Return the decrypted key for a provider, or None if not stored."""
    from app import crypto

    row = await pool.fetchrow(
        "SELECT ciphertext, nonce FROM nautgate.provider_credentials WHERE provider = $1",
        provider,
    )
    if row is None:
        return None
    return crypto.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]))


async def list_provider_credentials(pool: asyncpg.Pool) -> list[dict]:
    """Metadata only — provider, last4, updated_at. Never the plaintext."""
    rows = await pool.fetch(
        "SELECT provider, last4, updated_at FROM nautgate.provider_credentials ORDER BY provider"
    )
    out = []
    for r in rows:
        d = dict(r)
        d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
        out.append(d)
    return out


async def delete_provider_credential(pool: asyncpg.Pool, provider: str) -> bool:
    res = await pool.execute(
        "DELETE FROM nautgate.provider_credentials WHERE provider = $1", provider
    )
    return res.endswith(" 1")


# --- Compliance audit trace (NAUTGATE-25) ---------------------------------
# Fire-and-forget: the trace is an annotation on the audit log, never on the
# request path. A failed trace write must never affect the call it describes.


async def write_compliance_trace(pool: asyncpg.Pool, *, decision_id: UUID, trace: dict) -> None:
    """Record what one call touched. Upsert so a re-evaluation can rewrite it."""
    dest = trace.get("destination") or {}
    flags = trace.get("flags") or []
    await pool.execute(
        """
        INSERT INTO nautgate.compliance_traces
            (decision_id, activity, label, confidence, effect, data_class,
             evaluated_against, regimes_touched, destination, provider_terms,
             flags, flag_count)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12)
        ON CONFLICT (decision_id) DO UPDATE SET
            activity = EXCLUDED.activity, label = EXCLUDED.label,
            confidence = EXCLUDED.confidence, effect = EXCLUDED.effect,
            data_class = EXCLUDED.data_class,
            evaluated_against = EXCLUDED.evaluated_against,
            regimes_touched = EXCLUDED.regimes_touched,
            destination = EXCLUDED.destination,
            provider_terms = EXCLUDED.provider_terms,
            flags = EXCLUDED.flags, flag_count = EXCLUDED.flag_count
        """,
        decision_id,
        trace.get("activity") or "unknown",
        trace.get("label") or "O",
        trace.get("confidence") or "fallback",
        trace.get("effect"),
        trace.get("data_class") or "public",
        list(trace.get("evaluated_against") or []),
        list(trace.get("regimes_touched") or []),
        json.dumps(dest),
        json.dumps(trace.get("provider_terms") or {}),
        json.dumps(flags),
        len(flags),
    )


async def get_compliance_summary(pool: asyncpg.Pool, *, hours: int) -> dict:
    """KPI row for the Compliance page."""
    row = await pool.fetchrow(
        """
        SELECT COUNT(*)                                                   AS traced,
               COALESCE(SUM(flag_count), 0)                               AS flags,
               COUNT(*) FILTER (WHERE flag_count > 0)                     AS flagged_calls,
               COUNT(*) FILTER (WHERE (destination->>'third_country_transfer') = 'true')
                                                                          AS left_home,
               COUNT(*) FILTER (WHERE data_class IN ('personal','sensitive'))
                                                                          AS personal
          FROM nautgate.compliance_traces
         WHERE ts > NOW() - make_interval(hours => $1)
        """,
        hours,
    )
    return {
        "window_hours": hours,
        "traced": int((row or {}).get("traced") or 0),
        "flags": int((row or {}).get("flags") or 0),
        "flagged_calls": int((row or {}).get("flagged_calls") or 0),
        "left_home": int((row or {}).get("left_home") or 0),
        "personal": int((row or {}).get("personal") or 0),
    }


async def get_compliance_traces(
    pool: asyncpg.Pool, *, hours: int, only_flagged: bool = False, limit: int = 50
) -> list[dict]:
    """Recent traces joined to the decision they annotate."""
    rows = await pool.fetch(
        f"""
        SELECT c.decision_id, c.ts, c.activity, c.label, c.data_class,
               c.regimes_touched, c.destination, c.flags, c.flag_count,
               c.reviewed_at, d.agent_id, d.decision_model
          FROM nautgate.compliance_traces c
          JOIN nautgate.route_decisions d ON d.id = c.decision_id
         WHERE c.ts > NOW() - make_interval(hours => $1)
           {"AND c.flag_count > 0" if only_flagged else ""}
         ORDER BY c.ts DESC
         LIMIT $2
        """,
        hours,
        limit,
    )
    return [
        {
            "decision_id": str(r["decision_id"]),
            "ts": r["ts"].isoformat(),
            "agent_id": r["agent_id"],
            "model": r["decision_model"],
            "activity": r["activity"],
            "label": r["label"],
            "data_class": r["data_class"],
            "regimes_touched": list(r["regimes_touched"] or []),
            "destination": json.loads(r["destination"])
            if isinstance(r["destination"], str)
            else (r["destination"] or {}),
            "flags": json.loads(r["flags"]) if isinstance(r["flags"], str) else (r["flags"] or []),
            "flag_count": int(r["flag_count"] or 0),
            "reviewed": r["reviewed_at"] is not None,
        }
        for r in rows
    ]


async def get_compliance_trace(pool: asyncpg.Pool, decision_id: UUID) -> dict | None:
    """One trace in full, for the detail view."""
    r = await pool.fetchrow(
        """
        SELECT c.*, d.agent_id, d.decision_model, d.decision_provider, d.ts AS decision_ts
          FROM nautgate.compliance_traces c
          JOIN nautgate.route_decisions d ON d.id = c.decision_id
         WHERE c.decision_id = $1
        """,
        decision_id,
    )
    if r is None:
        return None
    j = lambda v: json.loads(v) if isinstance(v, str) else (v or {})  # noqa: E731
    return {
        "decision_id": str(r["decision_id"]),
        "ts": r["ts"].isoformat(),
        "agent_id": r["agent_id"],
        "model": r["decision_model"],
        "provider": r["decision_provider"],
        "activity": r["activity"],
        "label": r["label"],
        "confidence": r["confidence"],
        "effect": r["effect"],
        "data_class": r["data_class"],
        "evaluated_against": list(r["evaluated_against"] or []),
        "regimes_touched": list(r["regimes_touched"] or []),
        "destination": j(r["destination"]),
        "provider_terms": j(r["provider_terms"]),
        "flags": j(r["flags"]) or [],
        "reviewed": r["reviewed_at"] is not None,
    }


async def mark_compliance_reviewed(pool: asyncpg.Pool, decision_id: UUID, who: str) -> bool:
    res = await pool.execute(
        "UPDATE nautgate.compliance_traces SET reviewed_at = NOW(), reviewed_by = $2 "
        "WHERE decision_id = $1",
        decision_id,
        who,
    )
    return res.endswith(" 1")
