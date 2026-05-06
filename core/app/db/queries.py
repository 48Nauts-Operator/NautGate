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
