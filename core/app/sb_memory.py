"""SecondBrain memory ingest — post-outcome hook.

Direct port of flow-proxy's ``storeDelta`` so we can retire FlowAI once
this is wired up. Extracts the last user message + the assistant's text
from each completed decision and INSERTs into stargate's
``agents_memory.memories`` table. Fire-and-forget; a stargate outage
must never break a request.

Config (env vars, matching FlowAI's names so existing deploys
"just work"):
    NAUTGATE_SB_INGEST       'true' to enable (default off; opt-in)
    MEMORY_DB_HOST           stargate Tailscale IP (default 100.71.163.122)
    MEMORY_DB_PORT           5433
    MEMORY_DB_NAME           agents_memory
    MEMORY_DB_USER           agents
    MEMORY_DB_PASSWORD       agents_secure_2026

Schema (Stargate side, pre-existing):
    memories (
        id        bigserial primary key,
        agent_id  text,
        category  text,          -- 'user_message' | 'assistant_response'
        content   text,
        metadata  jsonb,
        created_at timestamptz default now()
    )
"""

from __future__ import annotations

import json
import time
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger()

# ── Config ──────────────────────────────────────────────────────────────────
# Resolved per-call from the DB (nautgate.app_config) with env fallback.
# Cached briefly so we don't hammer Postgres on every outcome write.

_config_cache: dict | None = None
_config_cached_at: float = 0.0
_CONFIG_CACHE_TTL_SEC = 10.0


async def _get_config(pool) -> dict:
    """Fetch and cache the live SB config (enabled flag + connection)."""
    global _config_cache, _config_cached_at
    now = time.monotonic()
    if _config_cache is not None and (now - _config_cached_at) < _CONFIG_CACHE_TTL_SEC:
        return _config_cache
    from app.app_config import is_offline, sb_ingest_config

    cfg = await sb_ingest_config(pool)
    # Offline / air-gapped: SB ingest usually points at a Postgres on ANOTHER
    # host, so it is real data leaving the box even though it isn't a model
    # provider. Report it disabled rather than connecting, so "this process
    # only opens sockets to localhost" holds without a second switch.
    if await is_offline(pool):
        cfg = {**cfg, "enabled": False}
    _config_cache = cfg
    _config_cached_at = now
    return cfg


def config_cache_clear() -> None:
    """Force the next call to re-read config from the DB. Called by the
    PUT /v1/config endpoint right after updating, so changes take effect
    on the very next request.
    """
    global _config_cache, _config_cached_at, _pool
    _config_cache = None
    _config_cached_at = 0.0
    # Close the old pool so the next request rebuilds it with the new DSN.
    _pool = None


def _dsn_from(cfg: dict) -> str:
    user = cfg.get("user") or "agents"
    pw = cfg.get("password") or ""
    host = cfg.get("host") or "100.71.163.122"
    port = cfg.get("port") or 5433
    name = cfg.get("database") or "agents_memory"
    return f"postgres://{user}:{pw}@{host}:{port}/{name}"


# ── Circuit breaker ─────────────────────────────────────────────────────────
# If stargate is unreachable, stop hammering it for a cooldown window.

_CIRCUIT_THRESHOLD = 5  # consecutive failures
_CIRCUIT_COOLDOWN_SEC = 60  # window to wait before retrying
_consecutive_failures = 0
_circuit_open_until_ts: float = 0.0


def _circuit_open() -> bool:
    return time.monotonic() < _circuit_open_until_ts


def _record_failure() -> None:
    global _consecutive_failures, _circuit_open_until_ts
    _consecutive_failures += 1
    if _consecutive_failures >= _CIRCUIT_THRESHOLD:
        _circuit_open_until_ts = time.monotonic() + _CIRCUIT_COOLDOWN_SEC
        log.warning(
            "sb_memory_circuit_open",
            failures=_consecutive_failures,
            cooldown_sec=_CIRCUIT_COOLDOWN_SEC,
        )


def _record_success() -> None:
    global _consecutive_failures, _circuit_open_until_ts
    _consecutive_failures = 0
    _circuit_open_until_ts = 0.0


# ── Lazy pool ───────────────────────────────────────────────────────────────
# Built on first use so app startup isn't slowed down. Closed in the lifespan
# shutdown via ``close_pool``.

_pool: asyncpg.Pool | None = None


async def _get_pool(cfg: dict) -> asyncpg.Pool | None:
    global _pool
    if _pool is not None:
        return _pool
    try:
        _pool = await asyncpg.create_pool(
            _dsn_from(cfg),
            min_size=1,
            max_size=4,
            command_timeout=5.0,
        )
        log.info("sb_memory_pool_ready", dsn_host=cfg.get("host"))
        return _pool
    except Exception as exc:
        _record_failure()
        log.warning("sb_memory_pool_init_failed", error=str(exc))
        return None


async def close_pool() -> None:
    """Called from the FastAPI lifespan shutdown hook."""
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        finally:
            _pool = None


# ── Delta extraction ────────────────────────────────────────────────────────


def _extract_text(content: Any) -> str:
    """Get plain text out of a content field that may be a string or a list
    of Anthropic-style content blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def _assistant_text_and_meta(
    response_body: dict | None,
    model: str | None,
    session_id: str | None,
) -> tuple[str, dict]:
    """Pull the assistant's text out of a captured response body, regardless
    of which API shape it came from (OpenAI Chat, OpenAI Responses, Anthropic).
    """
    if not isinstance(response_body, dict):
        return "", {}

    # OpenAI Chat Completions
    choices = response_body.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        text = msg.get("content") or ""
        usage = response_body.get("usage") or {}
        return text, {
            "session_id": session_id,
            "model": response_body.get("model") or model,
            "response_id": response_body.get("id"),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "stop_reason": (choices[0] or {}).get("finish_reason"),
            "source": "nautgate",
        }

    # OpenAI Responses API
    output = response_body.get("output")
    if isinstance(output, list) and output:
        parts: list[str] = []
        for o in output:
            for c in (o or {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
        usage = response_body.get("usage") or {}
        return "\n".join(parts), {
            "session_id": session_id,
            "model": response_body.get("model") or model,
            "response_id": response_body.get("id"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": response_body.get("status"),
            "source": "nautgate",
        }

    # Anthropic Messages
    content = response_body.get("content")
    if isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = response_body.get("usage") or {}
        return text, {
            "session_id": session_id,
            "model": response_body.get("model") or model,
            "response_id": response_body.get("id"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": response_body.get("stop_reason"),
            "source": "nautgate",
        }

    return "", {}


def _build_entries(
    *,
    agent_id: str,
    session_id: str | None,
    model: str | None,
    prompt_body: str | None,
    response_body: str | None,
) -> list[dict]:
    """Returns 0-2 memory entries: the last user turn + the assistant response.

    Mirrors flow-proxy's storeDelta filtering:
      - Skip user content that's only tool_results (no new user input)
      - Skip user text shorter than 5 chars
      - Skip assistant text shorter than 5 chars
      - Truncate user to 2000 chars, assistant to 4000
    """
    entries: list[dict] = []

    # ── User: last message, if it's actually a user turn with text ─────────
    messages: list | None = None
    if prompt_body:
        try:
            parsed = json.loads(prompt_body)
            messages = (
                parsed
                if isinstance(parsed, list)
                else (parsed.get("messages") if isinstance(parsed, dict) else None)
            )
        except (ValueError, TypeError):
            messages = None

    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            text = _extract_text(last.get("content"))
            is_tool_result_only = isinstance(last.get("content"), list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"]
            )
            if text and len(text) > 5 and not is_tool_result_only:
                entries.append(
                    {
                        "category": "user_message",
                        "content": text[:2000],
                        "metadata": {
                            "session_id": session_id,
                            "model": model,
                            "turn": len(messages),
                            "source": "nautgate",
                        },
                    }
                )

    # ── Assistant: parse the response body across formats ──────────────────
    if response_body:
        try:
            parsed_resp = json.loads(response_body)
        except (ValueError, TypeError):
            parsed_resp = None
        if parsed_resp:
            assistant_text, assistant_meta = _assistant_text_and_meta(
                parsed_resp,
                model=model,
                session_id=session_id,
            )
            if assistant_text and len(assistant_text) > 5:
                entries.append(
                    {
                        "category": "assistant_response",
                        "content": assistant_text[:4000],
                        "metadata": assistant_meta,
                    }
                )

    return entries


# ── Public API ──────────────────────────────────────────────────────────────


async def ingest_outcome(
    *,
    app_pool,  # NautGate's pool — used to load app_config
    agent_id: str,
    session_id: str | None,
    model: str | None,
    prompt_body: str | None,
    response_body: str | None,
) -> None:
    """Fire-and-forget: extract the user/assistant delta from a completed
    decision and write it to ``agents_memory.memories`` on stargate.

    Config is resolved at call time (DB > env > defaults) so toggling
    the Dashboard switch takes effect on the next request — no restart.
    """
    if _circuit_open():
        return
    cfg = await _get_config(app_pool)
    if not cfg.get("enabled"):
        return
    entries = _build_entries(
        agent_id=agent_id,
        session_id=session_id,
        model=model,
        prompt_body=prompt_body,
        response_body=response_body,
    )
    if not entries:
        return

    pool = await _get_pool(cfg)
    if pool is None:
        return

    try:
        async with pool.acquire() as conn:
            for e in entries:
                await conn.execute(
                    """
                    INSERT INTO memories (agent_id, category, content, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    agent_id,
                    e["category"],
                    e["content"],
                    json.dumps(e["metadata"]),
                )
        _record_success()
        log.info("sb_memory_ingested", agent_id=agent_id, entries=len(entries))
    except Exception as exc:
        _record_failure()
        log.warning("sb_memory_ingest_failed", error=str(exc), agent_id=agent_id)


async def test_connection(cfg: dict) -> tuple[bool, str]:
    """Open a one-shot connection to the configured SB DB and run a tiny
    query. Returns (ok, detail_or_error). Used by the 'Test' button in the
    Dashboard so the operator can verify creds before flipping the toggle.
    """
    try:
        conn = await asyncpg.connect(_dsn_from(cfg), timeout=5.0)
        try:
            row = await conn.fetchrow("SELECT COUNT(*) AS n FROM memories LIMIT 1")
            count = int(row["n"]) if row else 0
        finally:
            await conn.close()
        return True, f"connected · {count:,} memories in target DB"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
