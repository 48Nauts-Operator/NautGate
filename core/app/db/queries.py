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
                 actual_model, actual_provider,
                 notional_cost_usd, rate_limited_429)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                    $18::jsonb, $19, $20, $21, $22)
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
            notional_cost_usd,
            rate_limited_429,
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
        by_project = await _by("COALESCE(d.project_id, '(none)')") if not project_id or project_id == "*" else []

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
    }


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
            "total_cost_usd_30d": float(r["total_cost_usd_30d"] or 0) if r["total_cost_usd_30d"] else 0.0,
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
            did, judge_provider, judge_model, judge_cost_usd, judge_latency_ms,
            rubric_json, tags, suggested_prompt, coach_notes, trigger, anti_pattern,
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
    "over_thinking", "off_task", "looped", "hallucination",
    "partial_answer", "refusal", "tool_misuse",
]


async def get_quality_summary(
    pool: asyncpg.Pool, *, hours: int, model_filter: str | None = None,
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
            m, {"model": m, "buckets": {b: None for b in bucket_labels}, "counts": {b: 0 for b in bucket_labels}}
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
                "avg_completion": float(r["avg_completion"]) if r["avg_completion"] is not None else None,
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
