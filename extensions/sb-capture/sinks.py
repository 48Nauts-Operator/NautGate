"""sb-capture sinks — NDJSON + Postgres.

Each sink owns its own connection lifecycle. Sinks are non-blocking from the
hook handler's perspective (errors are logged, never raised) so a flapping
sink can never break the gateway's request path.

The Postgres sink mirrors Flow Proxy's `memories` schema:
    INSERT INTO memories (agent_id, category, content, metadata)
    VALUES ($1, $2, $3, $4::jsonb)

Categories used:
  - "user_message"        — last user prompt, extracted from on_request payload
  - "assistant_response"  — assistant text, extracted from on_response payload
  - "route_decision"      — the routing decision metadata (NautGate-specific)
  - "route_outcome"       — the outcome metrics (NautGate-specific)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("sb-capture.sinks")


class NDJSONSink:
    """Append-only NDJSON file sink. Cheap and dev-friendly; not durable on power loss."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def write(self, hook: str, body: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {"hook": hook, "received_at": time.time(), "payload": body},
                separators=(",", ":"),
                default=str,
            )
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            logger.warning("ndjson_write_failed", extra={"error": str(exc)})

    async def close(self) -> None:
        return None


class PostgresSink:
    """Writes captured text to agents_memory.memories in Flow-Proxy-compatible shape.

    Lazy-connects on first write; reuses the connection. On any error during write,
    logs and resets the connection so the next call retries cleanly.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            import asyncpg  # local import keeps NDJSON-only usage from needing the dep

            self._pool = await asyncpg.create_pool(
                dsn=self.dsn, min_size=1, max_size=4, command_timeout=5.0
            )
        return self._pool

    async def _insert(self, agent_id: str, category: str, content: str, metadata: dict) -> None:
        if not content or len(content) <= 5:
            return
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO memories (agent_id, category, content, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    agent_id,
                    category,
                    content,
                    json.dumps(metadata, default=str),
                )
        except Exception as exc:
            logger.warning(
                "memories_insert_failed",
                extra={"error": str(exc), "category": category},
            )
            # Drop the pool so the next call retries from scratch.
            await self.close()

    async def write(self, hook: str, body: dict) -> None:
        agent_id = body.get("agent_id") or "unknown"
        decision_id = body.get("decision_id")
        common_meta = {
            "decision_id": decision_id,
            "source": "nautgate-sb-capture",
            "inbound_format": body.get("inbound_format"),
        }

        if hook == "on_request":
            # Pull the last user message text from prompt_body (JSON-encoded messages list).
            text = _extract_last_user_text(body.get("prompt_body"))
            if text:
                await self._insert(
                    agent_id,
                    "user_message",
                    text[:2000],
                    {
                        **common_meta,
                        "model": body.get("model_requested"),
                        "decision_provider": body.get("decision_provider"),
                        "decision_model": body.get("decision_model"),
                        "classified_tier": body.get("classified_tier"),
                        "classified_sensitivity": body.get("classified_sensitivity"),
                    },
                )

        elif hook == "on_response":
            text = body.get("response_body")
            if isinstance(text, str) and text:
                # response_body may be a JSON-encoded dict or a raw string; try both.
                extracted = _extract_assistant_text(text)
                if extracted:
                    await self._insert(
                        agent_id,
                        "assistant_response",
                        extracted[:4000],
                        {
                            **common_meta,
                            "status_code": body.get("status_code"),
                            "prompt_tokens": body.get("prompt_tokens"),
                            "completion_tokens": body.get("completion_tokens"),
                            "was_empty": body.get("was_empty"),
                        },
                    )

        elif hook == "on_outcome":
            # Lightweight metric row — useful for dashboards, optional.
            await self._insert(
                agent_id,
                "route_outcome",
                f"decision={decision_id} status={body.get('status_code')} ms={body.get('duration_ms')}",
                {
                    **common_meta,
                    "status_code": body.get("status_code"),
                    "duration_ms": body.get("duration_ms"),
                    "prompt_tokens": body.get("prompt_tokens"),
                    "completion_tokens": body.get("completion_tokens"),
                    "was_empty": body.get("was_empty"),
                    "decision_provider": body.get("decision_provider"),
                    "decision_model": body.get("decision_model"),
                },
            )

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                with contextlib.suppress(Exception):
                    await self._pool.close()
                self._pool = None


# --- helpers ---------------------------------------------------------------


def _extract_last_user_text(prompt_body: Any) -> str:
    """prompt_body is the JSON-serialized messages list (Day 4c capture). Pull the
    last user message's text. Tolerates plain strings and content-block lists.
    """
    if not isinstance(prompt_body, str) or not prompt_body:
        return ""
    try:
        msgs = json.loads(prompt_body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(msgs, list):
        return ""
    for msg in reversed(msgs):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") in ("text", "input_text")
            ]
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
        return ""
    return ""


def _extract_assistant_text(response_body: str) -> str:
    """response_body is either a JSON-encoded provider response or a raw assembled
    string (streaming path). Extract the assistant text in any of the three shapes.
    """
    # Streaming path stored the assembled content as a plain string.
    if not response_body.lstrip().startswith(("{", "[")):
        return response_body

    try:
        obj = json.loads(response_body)
    except ValueError:
        return response_body

    # OpenAI Chat
    if isinstance(obj, dict) and isinstance(obj.get("choices"), list) and obj["choices"]:
        msg = obj["choices"][0].get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"]

    # OpenAI Responses
    if isinstance(obj, dict) and isinstance(obj.get("output_text"), str):
        return obj["output_text"]
    if isinstance(obj, dict) and isinstance(obj.get("output"), list):
        parts: list[str] = []
        for item in obj["output"]:
            for blk in item.get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text":
                    parts.append(blk.get("text", ""))
        if parts:
            return "\n".join(parts)

    # Anthropic Messages
    if isinstance(obj, dict) and isinstance(obj.get("content"), list):
        parts = [
            blk.get("text", "")
            for blk in obj["content"]
            if isinstance(blk, dict) and blk.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)

    return ""
