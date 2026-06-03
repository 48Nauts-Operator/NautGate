"""Anthropic OAuth passthrough — supports Claude Code on Max subscriptions.

When Claude Code is logged in via ``claude login``, it stores an OAuth
access token (format: ``sk-ant-oat01-...``) and sends it as
``Authorization: Bearer sk-ant-oat01-...`` on requests. Routing those
through NautRouter would bill the operator's metered Anthropic key
(in ``deploy/.env`` as ``ANTHROPIC_API_KEY``) — paying twice when the
Max subscription already covers them.

This module mirrors ``oauth_forwarder.py`` (the ChatGPT/Codex equivalent):
  - detect the OAuth bearer pattern (skip the ng_ auth gate)
  - forward the request body + headers to api.anthropic.com verbatim
  - stream the response back
  - write an audit row with ``decision_provider='anthropic-oauth'`` and a
    stable agent_id derived from the token suffix
  - capture body for the audit drawer (policy-gated)
  - compute ``notional_cost_usd`` (what it WOULD have cost on metered) so
    the dashboard can show subscription savings
  - flag ``rate_limited_429`` when the subscription hit its per-window cap

End state: NautGate sees every Claude call (full audit), pays $0 for them,
shows the operator what they saved.
"""

from __future__ import annotations

import json
import time as _time
import uuid
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.capture import capture_prompt, capture_response, capture_tools
from app.db import queries
from app.streaming import parse_sse_for_outcome

log = structlog.get_logger()

ANTHROPIC_HOST = "api.anthropic.com"
# Marker prefix Anthropic uses for OAuth Access Tokens (vs sk-ant-api03 for
# metered API keys). Detected case-insensitively via the bearer parser below.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat01-"

# Hop-by-hop headers per RFC — never forwarded between client and upstream.
_HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",   # httpx recomputes
    "content-encoding",  # let httpx pick / strip
}

# Response-side strip: we forward raw bytes via aiter_raw() (which does NOT
# decompress), so any Content-Encoding (gzip / br) must be preserved on
# the way back so the client decompresses. Same for Content-Length — the
# upstream value is correct for the bytes we relay.
_HOP_BY_HOP_RESPONSE = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
}


def is_anthropic_oauth_request(request: Request) -> bool:
    """Detect Claude Code (or any client) sending an Anthropic OAuth bearer.

    The signal is the ``Authorization: Bearer sk-ant-oat01-...`` token
    format. Also accepts the equivalent ``x-api-key`` form for clients
    that pack the OAuth token there (rare, but observed). We never look
    at the body — detection has to be cheap enough for every inbound.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token.lower().startswith(_OAUTH_TOKEN_PREFIX):
            return True
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key.lower().startswith(_OAUTH_TOKEN_PREFIX):
        return True
    return False


def _agent_id_for(token: str) -> str:
    """Derive a stable agent_id from the OAuth token without leaking it.

    Tokens look like ``sk-ant-oat01-<short>-<long>``. We take the first
    16 chars after the prefix so reissues bind to the same agent but
    distinct accounts get distinct ids.
    """
    body = token[len(_OAUTH_TOKEN_PREFIX):] if token.lower().startswith(_OAUTH_TOKEN_PREFIX) else token
    return f"claude-oauth-{body[:16]}"


def _build_forward_headers(request: Request) -> dict[str, str]:
    """Strip hop-by-hop headers; forward everything else verbatim. The
    Authorization / x-api-key header is what authenticates us to Anthropic.
    """
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def _notional_cost(pricing, model: str | None, prompt_tk: int | None,
                   completion_tk: int | None) -> float | None:
    """Compute what this call WOULD have cost on metered billing.

    Uses the same PricingTable the metered path uses, with ``anthropic``
    as the provider hint so snapshot model names (claude-opus-4-7, etc.)
    resolve via the YAML anchors in pricing.yaml.
    """
    if pricing is None or not model:
        return None
    try:
        return pricing.compute_cost(
            "anthropic", model,
            prompt_tokens=prompt_tk, completion_tokens=completion_tk,
        )
    except Exception:
        return None


def _parse_response_meta(body_bytes: bytes, was_streaming: bool) -> dict:
    """Extract token counts + tool calls from the upstream response.

    Streaming → re-use the existing SSE parser (it already understands
    Anthropic's message_start / message_delta usage events).
    Non-streaming → just decode the JSON envelope.
    Returns a dict with keys: prompt_tokens, completion_tokens,
    reasoning_tokens, tool_calls, assembled_content.
    """
    if was_streaming:
        try:
            return parse_sse_for_outcome(body_bytes)
        except Exception:
            return {}
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage") or {}
    content = payload.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}),
                })
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (usage.get("cache_read_input_tokens")
                             or usage.get("cache_creation_input_tokens")),
        "tool_calls": tool_calls,
        "assembled_content": "".join(text_parts),
    }


async def forward_to_anthropic(request: Request) -> StreamingResponse | JSONResponse:
    """Forward an OAuth-authenticated Claude request to api.anthropic.com.

    Mirrors the chat_completions / messages flow: PRECAPTURE row before
    forwarding, outcome row in the streaming finally block, full body
    capture for the audit drawer.
    """
    raw_body = await request.body()
    payload: dict | None = None
    try:
        payload = json.loads(raw_body) if raw_body else None
    except (ValueError, TypeError):
        payload = None  # binary / malformed — still forward

    # Identify the caller from their OAuth token. We never log or persist
    # the token; only the derived short id ends up in agent_id.
    auth_hdr = request.headers.get("authorization", "")
    token = auth_hdr.split(" ", 1)[1].strip() if auth_hdr.lower().startswith("bearer ") else \
        request.headers.get("x-api-key", "").strip()
    agent_id = _agent_id_for(token)

    decision_id = uuid.uuid4()
    requested_model = (payload or {}).get("model") if isinstance(payload, dict) else None
    inbound_format = "anthropic_messages_oauth"
    is_stream = bool(isinstance(payload, dict) and payload.get("stream"))

    pool = getattr(request.app.state, "db", None)
    pricing = getattr(request.app.state, "pricing", None)
    started_at_ns = _time.monotonic_ns()

    # PRECAPTURE — synchronous, before forwarding (durability §9).
    if pool is not None:
        try:
            from app.scoring import score
            score_vector = score(payload or {})
            messages = (payload or {}).get("messages") if isinstance(payload, dict) else None
            captured_body = capture_prompt(messages, "none") if messages else None
            captured_tools = (
                capture_tools(payload.get("tools"), "none")
                if isinstance(payload, dict) and payload.get("tools") else None
            )
            await queries.precapture(
                pool,
                decision_id=decision_id,
                agent_id=agent_id,
                inbound_format=inbound_format,
                model_requested=requested_model,
                classified_tier="passthrough",
                classified_score=score_vector.aggregate,
                classified_sensitivity="none",
                decision_provider="anthropic-oauth",
                decision_model=requested_model or "claude-default",
                decision_reason="anthropic-oauth:passthrough",
                prompt_body=captured_body.body if captured_body else None,
                prompt_body_truncated_at_byte=(
                    captured_body.truncated_at_byte if captured_body else None
                ),
                tools_body=captured_tools.body if captured_tools else None,
                tools_body_truncated_at_byte=(
                    captured_tools.truncated_at_byte if captured_tools else None
                ),
                source_ip=request.client.host if request.client else None,
                source_hostname=request.headers.get("x-forwarded-host"),
                messages_count=len(messages) if isinstance(messages, list) else None,
                tools_count=(
                    len(payload["tools"])
                    if isinstance(payload, dict) and isinstance(payload.get("tools"), list)
                    else None
                ),
                stream_flag=is_stream,
                request_size_bytes=len(raw_body),
            )
        except Exception as exc:
            log.warning("anthropic_oauth_precapture_failed", error=str(exc))

    # Forward. Use a per-request client so we don't tie up the global pool
    # with long SSE streams.
    fwd_headers = _build_forward_headers(request)
    # Anthropic requires the api version header — Claude Code already
    # attaches it, but be defensive in case some client doesn't.
    fwd_headers.setdefault("anthropic-version", "2023-06-01")
    # Path is whatever the client called us with, on api.anthropic.com.
    url = f"https://{ANTHROPIC_HOST}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), http2=False)
    try:
        upstream = await client.send(
            client.build_request(
                method=request.method, url=url,
                headers=fwd_headers, content=raw_body,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        log.error("anthropic_oauth_forward_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"anthropic_unreachable: {exc}") from None

    # Build the response back to the client. Preserve Content-Encoding
    # so the client decompresses correctly (we forward raw upstream bytes).
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP_RESPONSE
    }
    response_headers["X-Nautgate-Decision-Id"] = str(decision_id)
    response_headers["X-Nautgate-OAuth-Passthrough"] = "anthropic"

    upstream_status = upstream.status_code
    was_rate_limited = upstream_status == 429

    async def _relay() -> AsyncIterator[bytes]:
        body_buf = bytearray()
        try:
            async for chunk in upstream.aiter_raw():
                body_buf.extend(chunk)
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            await client.aclose()

            if pool is not None:
                duration_ms = int((_time.monotonic_ns() - started_at_ns) / 1_000_000)
                try:
                    # We forward upstream bytes raw; for our own parsing we
                    # need to decompress if the upstream used gzip / br.
                    ce = upstream.headers.get("content-encoding", "").lower()
                    if ce in ("gzip", "x-gzip") and not is_stream:
                        import gzip
                        try:
                            decoded = gzip.decompress(bytes(body_buf))
                        except OSError:
                            decoded = bytes(body_buf)
                    elif ce == "br" and not is_stream:
                        try:
                            import brotli  # optional dep
                            decoded = brotli.decompress(bytes(body_buf))
                        except Exception:
                            decoded = bytes(body_buf)
                    else:
                        decoded = bytes(body_buf)
                    meta = _parse_response_meta(decoded, is_stream)
                    # Capture body for the audit drawer (policy gate is "none"
                    # — this path is already opt-in via OAuth detection).
                    response_text = decoded.decode("utf-8", errors="replace")
                    response_captured = capture_response(response_text, "none")
                    # Notional cost: what this call WOULD have cost on metered.
                    notional = _notional_cost(
                        pricing,
                        meta.get("actual_model") or requested_model,
                        meta.get("prompt_tokens"),
                        meta.get("completion_tokens"),
                    )
                    from app.outcome import persist_outcome
                    spool = getattr(request.app.state, "outcome_spool", None)
                    await persist_outcome(
                        pool, spool,
                        decision_id=decision_id,
                        status_code=upstream_status,
                        duration_ms=duration_ms,
                        prompt_tokens=meta.get("prompt_tokens"),
                        completion_tokens=meta.get("completion_tokens"),
                        reasoning_tokens=meta.get("reasoning_tokens"),
                        # Real spend = $0 (Max covers it). Notional separately.
                        cost_usd=0.0,
                        notional_cost_usd=notional,
                        rate_limited_429=was_rate_limited,
                        response_body=response_captured.body,
                        response_body_truncated_at_byte=response_captured.truncated_at_byte,
                        response_size_bytes=len(body_buf),
                        tool_calls_made=meta.get("tool_calls") or None,
                        actual_model=meta.get("actual_model") or requested_model,
                        actual_provider="anthropic-oauth",
                    )
                except Exception as exc:
                    log.warning("anthropic_oauth_outcome_failed", error=str(exc))

    return StreamingResponse(
        _relay(),
        status_code=upstream_status,
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
