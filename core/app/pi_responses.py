"""Isolated OpenAI Responses passthrough — Pi-specific, touches nothing else.

Why this exists: OpenAI forbids `tools + reasoning_effort` on
`/v1/chat/completions` but allows it on `/v1/responses`. Pi's Fusion builder
(gpt-5.6-sol, 56 tools, high reasoning) therefore fails on the normal routed
chat/completions path. Pi's *native* openai provider already speaks the
Responses shape; this endpoint lets Pi keep doing that through NautGate.

Design: PURE ROUTING. The Responses request is forwarded byte-for-byte to
OpenAI's `/v1/responses`, and the response streams back byte-for-byte. No
translation, no NautRouter, no shared code — a self-contained forwarder on a
dedicated route (`POST /pi/v1/responses`). Registered with one line in main.py.
Deleting this file + that line removes it completely; every existing route is
unaffected.

Only Pi reaches it (its `nautgate-oai` provider points here). Claude Code
(`/v1/messages`) and Codex (`/v1/responses`-OAuth) never touch this path.
"""

from __future__ import annotations

import json
import time as _time
import uuid

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import authenticate

log = structlog.get_logger()

router = APIRouter()

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def _as_messages(payload: dict) -> list[dict]:
    """Normalise a Responses `input` into chat-style [{role, content}].

    Responses allows `input` as a bare string or a list of items whose content
    is a string or a list of typed blocks. Everything downstream (session id,
    excerpt, classification) wants the flat chat shape. Never raises.
    """
    inp = payload.get("input")
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    out: list[dict] = []
    if isinstance(inp, list):
        for m in inp:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = "\n".join(
                    b["text"] for b in c if isinstance(b, dict) and isinstance(b.get("text"), str)
                )
            else:
                continue
            out.append({"role": m.get("role") or "user", "content": text})
    return out


def _extract_prompt_text(payload: dict) -> str:
    """All prompt text, for sensitivity classification."""
    return "\n".join(m["content"] for m in _as_messages(payload))


def _last_user_excerpt(payload: dict, limit: int = 200) -> str | None:
    """First `limit` chars of the last user turn — same convention as the other
    capture paths. Doubles as the head-to-head correlation key: two models asked
    the same question yield the same excerpt."""
    for m in reversed(_as_messages(payload)):
        if m["role"] == "user" and m["content"].strip():
            return m["content"][:limit]
    return None


def _tool_calls_from_output(obj: dict) -> list[dict] | None:
    """Function calls the model made, from a Responses `output` array."""
    calls = [
        {"name": it.get("name"), "id": it.get("call_id") or it.get("id")}
        for it in (obj.get("output") or [])
        if isinstance(it, dict) and it.get("type") == "function_call"
    ]
    return calls or None


@router.post("/pi/v1/responses", response_model=None)
async def pi_responses(request: Request):
    """Verbatim passthrough of an OpenAI Responses request to api.openai.com,
    with an audit row. Pi-only; bypasses NautRouter and all translation."""
    import os as _os

    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)

    api_key = _os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="openai_key_unconfigured")

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    requested_model = payload.get("model")
    is_stream = bool(payload.get("stream"))
    decision_id = uuid.uuid4()
    started_ns = _time.monotonic_ns()

    # PRECAPTURE — synchronous audit row before forwarding (same as other paths).
    if pool is not None:
        try:
            from app.classify import classify
            from app.drift import compute_session_id

            classification = classify(_extract_prompt_text(payload))
            sensitivity = classification.sensitivity
            body_for_capture = (
                raw_body.decode("utf-8", errors="replace") if sensitivity != "secret" else None
            )
            tools = payload.get("tools")
            from app.db import queries

            await queries.precapture(
                pool,
                decision_id=decision_id,
                agent_id=agent_id,
                inbound_format="openai_responses_passthrough",
                model_requested=requested_model,
                classified_tier="passthrough",
                classified_sensitivity=sensitivity,
                classified_signals=classification.signals,
                decision_provider="openai-responses",
                decision_model=requested_model or "openai-default",
                decision_reason="pi:responses-passthrough",
                prompt_body=body_for_capture,
                source_ip=request.client.host if request.client else None,
                source_hostname=request.headers.get("x-forwarded-host"),
                prompt_excerpt=_last_user_excerpt(payload),
                tools_count=len(tools) if isinstance(tools, list) else None,
                messages_count=len(_as_messages(payload)) or None,
                stream_flag=is_stream,
                request_size_bytes=len(raw_body),
                session_id=compute_session_id(agent_id, _as_messages(payload)),
            )
        except Exception as exc:
            log.warning(
                "pi_responses_precapture_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": request.headers.get(
            "accept", "text/event-stream" if is_stream else "application/json"
        ),
    }
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), http2=False)

    async def _finish_audit(status: int, body_buf: bytearray, first_byte_ns: int | None) -> None:
        """Write the outcome row (fire-and-forget). Best-effort usage parse."""
        if pool is None:
            return
        try:
            decoded = bytes(body_buf).decode("utf-8", errors="replace")
            pt = ct = rt = None
            actual_model = requested_model
            tool_calls = None
            # Responses usage arrives as a `response.completed` event (stream)
            # or top-level `usage` (non-stream). Best-effort — audit only.
            try:
                if is_stream:
                    for line in decoded.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            frag = line[5:].strip()
                            if frag and frag != "[DONE]" and '"usage"' in frag:
                                obj = json.loads(frag)
                                resp = obj.get("response") or obj
                                u = resp.get("usage") or {}
                                pt = u.get("input_tokens", pt)
                                ct = u.get("output_tokens", ct)
                                rt = (
                                    (u.get("output_tokens_details") or {}).get("reasoning_tokens")
                                ) or rt
                                actual_model = resp.get("model") or actual_model
                                tool_calls = _tool_calls_from_output(resp) or tool_calls
                else:
                    obj = json.loads(decoded)
                    u = obj.get("usage") or {}
                    pt, ct = u.get("input_tokens"), u.get("output_tokens")
                    rt = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
                    actual_model = obj.get("model") or actual_model
                    tool_calls = _tool_calls_from_output(obj)
            except (ValueError, TypeError, AttributeError):
                pass
            duration_ms = int((_time.monotonic_ns() - started_ns) / 1_000_000)
            first_byte_ms = int((first_byte_ns - started_ns) / 1_000_000) if first_byte_ns else None
            pricing = getattr(request.app.state, "pricing", None)
            cost = (
                pricing.compute_cost(
                    "openai", requested_model, prompt_tokens=pt, completion_tokens=ct
                )
                if pricing
                else None
            )
            from app.capture import capture_response
            from app.outcome import persist_outcome
            from app.usage import cache_prefix_hash

            captured = capture_response(decoded, "none")
            await persist_outcome(
                pool,
                getattr(request.app.state, "outcome_spool", None),
                decision_id=decision_id,
                status_code=status,
                duration_ms=duration_ms,
                first_byte_ms=first_byte_ms,
                prompt_tokens=pt,
                completion_tokens=ct,
                reasoning_tokens=rt,
                response_body=captured.body,
                response_body_truncated_at_byte=captured.truncated_at_byte,
                response_size_bytes=len(body_buf),
                actual_model=actual_model,
                actual_provider="openai",
                cost_usd=cost,
                prefix_hash=cache_prefix_hash(payload),
                tool_calls_made=tool_calls,
                was_empty=bool(status and 200 <= status < 300 and not ct),
            )
        except Exception as exc:
            log.warning(
                "pi_responses_outcome_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )

    # --- Streaming passthrough ---
    if is_stream:

        async def _relay():
            body_buf = bytearray()
            first_byte_ns = None
            status = 0
            try:
                async with client.stream(
                    "POST", OPENAI_RESPONSES_URL, headers=headers, content=raw_body
                ) as upstream:
                    status = upstream.status_code
                    async for chunk in upstream.aiter_bytes():
                        if first_byte_ns is None:
                            first_byte_ns = _time.monotonic_ns()
                        body_buf.extend(chunk)
                        yield chunk
            finally:
                await client.aclose()
                await _finish_audit(status, body_buf, first_byte_ns)

        return StreamingResponse(_relay(), media_type="text/event-stream")

    # --- Non-streaming passthrough ---
    try:
        upstream = await client.post(OPENAI_RESPONSES_URL, headers=headers, content=raw_body)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.warning(
            "pi_responses_upstream_error",
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="openai_responses_unreachable") from None
    body = upstream.content
    await client.aclose()
    await _finish_audit(upstream.status_code, bytearray(body), _time.monotonic_ns())
    try:
        content = json.loads(body) if body else {}
    except (ValueError, TypeError):
        content = {}
    return JSONResponse(content=content, status_code=upstream.status_code)
