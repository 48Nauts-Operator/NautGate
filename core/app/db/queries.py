"""Query layer. Synchronous PRECAPTURE writes (audit log) and outcome writes.

The ledger durability contract (Tech Paper §9) requires `route_decisions` to be written
synchronously *before* upstream forward; `route_outcomes` is also synchronous on healthy
DB and gets a durable-spool fallback (Day 4d).
"""

import json
from uuid import UUID

import asyncpg


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
                 stream_flag, request_size_bytes, session_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb,
                    $12, $13, $14, $15, $16,
                    $17, $18,
                    $19::inet, $20, $21, $22, $23, $24, $25)
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
) -> None:
    tool_calls_json = json.dumps(tool_calls_made) if tool_calls_made else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.route_outcomes
                (decision_id, status_code, duration_ms, first_byte_ms,
                 prompt_tokens, completion_tokens, reasoning_tokens, cost_usd,
                 was_empty, used_fallback, fallback_count, client_disconnected,
                 was_truncated, truncated_at_byte,
                 response_body, response_body_truncated_at_byte,
                 response_size_bytes, tool_calls_made,
                 actual_model, actual_provider)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                    $18::jsonb, $19, $20)
            """,
            decision_id,
            status_code,
            duration_ms,
            first_byte_ms,
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
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
    agent_id: str,
    hours: int,
) -> dict:
    """Aggregate cost over the last N hours, broken down by provider/model/tier."""
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT COUNT(*)                          AS total_calls,
                   SUM(o.cost_usd)::FLOAT            AS total_cost_usd,
                   SUM(o.prompt_tokens)::BIGINT      AS total_prompt_tokens,
                   SUM(o.completion_tokens)::BIGINT  AS total_completion_tokens
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
             WHERE d.agent_id = $1
               AND d.ts > NOW() - make_interval(hours => $2)
            """,
            agent_id,
            hours,
        )

        async def _by(field_sql: str):
            rows = await conn.fetch(
                f"""
                SELECT {field_sql}                   AS k,
                       SUM(o.cost_usd)::FLOAT        AS cost_usd,
                       COUNT(*)                       AS calls,
                       SUM(o.prompt_tokens)::BIGINT  AS prompt_tokens,
                       SUM(o.completion_tokens)::BIGINT AS completion_tokens
                  FROM nautgate.route_decisions d
                  LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
                 WHERE d.agent_id = $1
                   AND d.ts > NOW() - make_interval(hours => $2)
                 GROUP BY {field_sql}
                 ORDER BY cost_usd DESC NULLS LAST
                """,
                agent_id,
                hours,
            )
            return [
                {
                    "key": r["k"],
                    "cost_usd": r["cost_usd"],
                    "calls": int(r["calls"]),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                }
                for r in rows
            ]

        by_provider = await _by("d.decision_provider")
        by_model = await _by("d.decision_model")
        by_tier = await _by("d.classified_tier")

    return {
        "agent_id": agent_id,
        "window_hours": hours,
        "total_calls": int((totals or {}).get("total_calls") or 0),
        "total_cost_usd": (totals or {}).get("total_cost_usd"),
        "total_prompt_tokens": int((totals or {}).get("total_prompt_tokens") or 0),
        "total_completion_tokens": int((totals or {}).get("total_completion_tokens") or 0),
        "by_provider": by_provider,
        "by_model": by_model,
        "by_tier": by_tier,
    }


async def get_cost_timeseries(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    bucket: str,
    hours: int,
) -> dict:
    """Bucketed cost series suitable for a line chart.

    Returns:
        {
            "agent_id": ...,
            "bucket": "hour" | "day",
            "window_hours": N,
            "series": [
                {"provider": "anthropic", "points": [{"ts": "...", "cost_usd": 0.01, "calls": 3}, ...]},
                ...
            ],
        }

    `bucket` MUST be one of {"hour", "day"} — caller validates and we trust it.
    """
    bucket = bucket if bucket in ("hour", "day") else "hour"
    rows = await pool.fetch(
        f"""
        SELECT date_trunc('{bucket}', d.ts) AS bucket_ts,
               d.decision_provider          AS provider,
               SUM(o.cost_usd)::FLOAT       AS cost_usd,
               COUNT(*)                      AS calls
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE d.agent_id = $1
           AND d.ts > NOW() - make_interval(hours => $2)
         GROUP BY bucket_ts, provider
         ORDER BY bucket_ts ASC
        """,
        agent_id,
        hours,
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
        "agent_id": agent_id,
        "bucket": bucket,
        "window_hours": hours,
        "series": [{"provider": p, "points": points} for p, points in series_map.items()],
    }


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
               o.actual_provider               AS actual_provider
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
    for k in ("classified_signals", "brain_hints", "tool_calls_made"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except (ValueError, TypeError):
                pass
    # Token breakdown computed on read from prompt_body when body was captured.
    d["token_estimate"] = _token_breakdown_from_body(d.get("prompt_body"), _content_text, _estimate_tokens)
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
        sys_section["bytes"] + tools_section["bytes"] + history_section["bytes"] + user_section["bytes"]
    )
    total_tokens = (
        sys_section["tokens"] + tools_section["tokens"] + history_section["tokens"] + user_section["tokens"]
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
    messages = data if isinstance(data, list) else data.get("messages") if isinstance(data, dict) else None
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
