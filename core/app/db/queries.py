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
) -> None:
    """Insert the audit row before forwarding upstream. Synchronous by design."""
    signals_json = json.dumps(classified_signals) if classified_signals else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.route_decisions
                (id, agent_id, inbound_format, model_requested, prompt_excerpt,
                 prompt_tokens, classified_tier, classified_score,
                 classified_sensitivity, classified_signals,
                 decision_provider, decision_model, decision_reason,
                 prompt_body, prompt_body_truncated_at_byte)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15)
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
            decision_provider,
            decision_model,
            decision_reason,
            prompt_body,
            prompt_body_truncated_at_byte,
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
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.route_outcomes
                (decision_id, status_code, duration_ms, first_byte_ms,
                 prompt_tokens, completion_tokens, reasoning_tokens, cost_usd,
                 was_empty, used_fallback, fallback_count, client_disconnected,
                 was_truncated, truncated_at_byte,
                 response_body, response_body_truncated_at_byte)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
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
        )


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
