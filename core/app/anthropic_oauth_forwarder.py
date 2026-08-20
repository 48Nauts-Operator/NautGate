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

import asyncio
import hashlib
import json
import time as _time
import uuid
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.audit_receipt import content_hash
from app.capture import capture_prompt, capture_response, capture_tools
from app.db import queries
from app.streaming import parse_sse_for_outcome
from app.usage import cache_prefix_hash, normalize_usage

log = structlog.get_logger()

# Overload-retry policy (529 capacity-shedding). Capped exponential backoff.
_MAX_OVERLOAD_RETRIES = 3
_RETRY_BASE_S = 0.4
_RETRY_CAP_S = 4.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds form). Ignores HTTP-date form."""
    if not value:
        return None
    try:
        secs = float(value.strip())
        return max(0.0, min(secs, _RETRY_CAP_S))
    except (ValueError, TypeError):
        return None


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    """Backoff before the next overload retry.

    Retry-After can only ever LENGTHEN the wait. Anthropic answers some 529s
    with `retry-after: 0`, and honouring that verbatim fired all four attempts
    inside two seconds, straight back into the same overload window, so the
    client saw a 529 the retry loop was supposed to absorb (2026-08-18: every
    retry in the burst logged delay_s 0.0).
    """
    backoff = min(_RETRY_CAP_S, _RETRY_BASE_S * (2**attempt)) + 0.05 * attempt
    return backoff if retry_after is None else max(backoff, retry_after)


ANTHROPIC_HOST = "api.anthropic.com"
# Marker prefix Anthropic uses for OAuth Access Tokens (vs sk-ant-api03 for
# metered API keys). Detected case-insensitively via the bearer parser below.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat01-"

# Hop-by-hop headers per RFC — never forwarded between client and upstream.
_HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",  # httpx recomputes
    "content-encoding",  # let httpx pick / strip
}

# Response-side strip: we forward raw bytes via aiter_raw() (which does NOT
# decompress), so any Content-Encoding (gzip / br) must be preserved on
# the way back so the client decompresses. Same for Content-Length — the
# upstream value is correct for the bytes we relay.
_HOP_BY_HOP_RESPONSE = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
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
    body = (
        token[len(_OAUTH_TOKEN_PREFIX) :]
        if token.lower().startswith(_OAUTH_TOKEN_PREFIX)
        else token
    )
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


def _notional_cost(
    pricing,
    model: str | None,
    prompt_tk: int | None,
    completion_tk: int | None,
    cache_read_tk: int | None = None,
    cache_write_tk: int | None = None,
) -> float | None:
    """Compute what this call WOULD have cost on metered billing.

    Uses the same PricingTable the metered path uses, with ``anthropic``
    as the provider hint so snapshot model names (claude-opus-4-7, etc.)
    resolve via the YAML anchors in pricing.yaml. Includes the cache tiers so
    the subscription-savings figure reflects the real (cache-discounted) bill.
    """
    if pricing is None or not model:
        return None
    try:
        return pricing.compute_cost(
            "anthropic",
            model,
            prompt_tokens=prompt_tk,
            completion_tokens=completion_tk,
            cache_read_tokens=cache_read_tk,
            cache_write_tokens=cache_write_tk,
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
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    }
                )
    n = normalize_usage(usage, provider_hint="anthropic")
    return {
        "prompt_tokens": n.prompt_tokens,
        "completion_tokens": n.completion_tokens,
        "reasoning_tokens": n.reasoning_tokens,
        "cache_read_tokens": n.cache_read_tokens,
        "cache_write_tokens": n.cache_write_tokens,
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
    token = (
        auth_hdr.split(" ", 1)[1].strip()
        if auth_hdr.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "").strip()
    )
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
            from app.classify import assemble_user_text, classify
            from app.drift import compute_session_id
            from app.scoring import score

            score_vector = score(payload or {})
            messages = (payload or {}).get("messages") if isinstance(payload, dict) else None
            # NAUTGATE-1: the passthrough path used to hardcode sensitivity="none",
            # so the Privacy/Lighthouse audit was blind on all OAuth (Max) traffic
            # — 84% of volume. Run the same classifier the routed path uses; its
            # result gates body capture and drives the sensitivity/signals columns.
            classification = classify(assemble_user_text(messages))
            sensitivity = classification.sensitivity
            captured_body = capture_prompt(messages, sensitivity) if messages else None
            captured_tools = (
                capture_tools(payload.get("tools"), sensitivity)
                if isinstance(payload, dict) and payload.get("tools")
                else None
            )
            await queries.precapture(
                pool,
                decision_id=decision_id,
                agent_id=agent_id,
                inbound_format=inbound_format,
                model_requested=requested_model,
                classified_tier="passthrough",
                classified_score=score_vector.aggregate,
                classified_sensitivity=sensitivity,
                classified_signals=classification.signals,
                session_id=compute_session_id(agent_id, messages),
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
            log.warning(
                "anthropic_oauth_precapture_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )

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
    # Transparent retry on 529/Overloaded (and 429) — Anthropic sheds ~1-in-4
    # requests under load; a couple of retries clears almost all of them before
    # the client ever sees an error. Safe because the status is known at headers,
    # before any body is streamed, and raw_body is already buffered.
    overload_retries = 0
    try:
        for _attempt in range(_MAX_OVERLOAD_RETRIES + 1):
            upstream = await client.send(
                client.build_request(
                    method=request.method,
                    url=url,
                    headers=fwd_headers,
                    content=raw_body,
                ),
                stream=True,
            )
            if upstream.status_code not in (429, 529) or _attempt == _MAX_OVERLOAD_RETRIES:
                break
            retry_after = _parse_retry_after(upstream.headers.get("retry-after"))
            if upstream.status_code == 529:
                overload_retries += 1
            await upstream.aclose()  # drain the failed response before retrying
            delay = _retry_delay(_attempt, retry_after)
            log.info(
                "anthropic_oauth_retry",
                status=upstream.status_code,
                attempt=_attempt + 1,
                delay_s=round(delay, 2),
            )
            await asyncio.sleep(delay)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.error(
            "anthropic_oauth_forward_failed",
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail=f"anthropic_unreachable: {exc}") from None

    # Build the response back to the client. Preserve Content-Encoding
    # so the client decompresses correctly (we forward raw upstream bytes).
    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE
    }
    response_headers["X-Nautgate-Decision-Id"] = str(decision_id)
    response_headers["X-Nautgate-OAuth-Passthrough"] = "anthropic"
    if overload_retries:
        response_headers["X-Nautgate-Overload-Retries"] = str(overload_retries)

    upstream_status = upstream.status_code
    was_rate_limited = upstream_status == 429

    async def _relay() -> AsyncIterator[bytes]:
        body_buf = bytearray()
        first_byte_ns: int | None = None
        try:
            async for chunk in upstream.aiter_raw():
                if first_byte_ns is None and chunk:
                    first_byte_ns = _time.monotonic_ns()
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
                first_byte_ms = (
                    int((first_byte_ns - started_at_ns) / 1_000_000)
                    if first_byte_ns is not None
                    else None
                )
                try:
                    # We forward upstream bytes raw to the client (so the
                    # client decompresses correctly). For our own parsing
                    # + audit-log storage we need to decompress here too,
                    # for both streaming AND non-streaming — Anthropic
                    # gzips Claude Code's SSE streams, and the previous
                    # guard left compressed garbage in response_body.
                    ce = upstream.headers.get("content-encoding", "").lower()
                    if ce in ("gzip", "x-gzip"):
                        import gzip

                        try:
                            decoded = gzip.decompress(bytes(body_buf))
                        except OSError:
                            decoded = bytes(body_buf)
                    elif ce == "br":
                        try:
                            import brotli  # optional dep

                            decoded = brotli.decompress(bytes(body_buf))
                        except Exception:
                            decoded = bytes(body_buf)
                    elif ce == "zstd":
                        try:
                            import zstandard  # optional dep

                            decoded = zstandard.ZstdDecompressor().decompress(bytes(body_buf))
                        except Exception:
                            decoded = bytes(body_buf)
                    else:
                        decoded = bytes(body_buf)
                    meta = _parse_response_meta(decoded, is_stream)
                    # Capture body for the audit drawer (policy gate is "none"
                    # — this path is already opt-in via OAuth detection).
                    response_text = decoded.decode("utf-8", errors="replace")
                    response_captured = capture_response(response_text, "none")
                    # Notional cost: what this call WOULD have cost on metered,
                    # cache tiers included.
                    notional = _notional_cost(
                        pricing,
                        meta.get("actual_model") or requested_model,
                        meta.get("prompt_tokens"),
                        meta.get("completion_tokens"),
                        meta.get("cache_read_tokens"),
                        meta.get("cache_write_tokens"),
                    )
                    from app.outcome import persist_outcome

                    spool = getattr(request.app.state, "outcome_spool", None)
                    await persist_outcome(
                        pool,
                        spool,
                        decision_id=decision_id,
                        status_code=upstream_status,
                        duration_ms=duration_ms,
                        first_byte_ms=first_byte_ms,
                        prompt_tokens=meta.get("prompt_tokens"),
                        completion_tokens=meta.get("completion_tokens"),
                        reasoning_tokens=meta.get("reasoning_tokens"),
                        cache_read_tokens=meta.get("cache_read_tokens"),
                        cache_write_tokens=meta.get("cache_write_tokens"),
                        prefix_hash=cache_prefix_hash(payload),
                        # Real spend = $0 (Max covers it). Notional separately.
                        cost_usd=0.0,
                        notional_cost_usd=notional,
                        rate_limited_429=was_rate_limited,
                        upstream_overload_retries=overload_retries,
                        response_body=response_captured.body,
                        response_body_truncated_at_byte=response_captured.truncated_at_byte,
                        response_size_bytes=len(body_buf),
                        tool_calls_made=meta.get("tool_calls") or None,
                        actual_model=meta.get("actual_model") or requested_model,
                        actual_provider="anthropic-oauth",
                        evidence={
                            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
                            "upstream_body_sha256": hashlib.sha256(raw_body).hexdigest(),
                            "prompt_sha256": content_hash(
                                (payload or {}).get("messages")
                                if isinstance(payload, dict)
                                else None
                            ),
                            "tools_sha256": content_hash(
                                (payload or {}).get("tools") if isinstance(payload, dict) else None
                            ),
                            "response_sha256": hashlib.sha256(decoded).hexdigest(),
                            "nautgate_key_id": "anthropic-oauth:"
                            + hashlib.sha256(token.encode()).hexdigest()[:16],
                            "selected_transport": "anthropic-oauth",
                            "finish_reason": meta.get("finish_reason"),
                            "error_code": (
                                None
                                if 200 <= upstream_status < 300
                                else f"upstream_http_{upstream_status}"
                            ),
                        },
                    )
                    # Quality eval — same fire-and-forget pattern as the
                    # main routing path. Captures Claude Code's actual prompts
                    # so the anti-pattern leaderboard sees real usage, not
                    # just whatever Pi sends through openrouter.
                    try:
                        from app.quality_eval import (
                            process_quality as _process_quality,
                        )

                        await _process_quality(
                            pool,
                            decision_id=decision_id,
                            judge_client=getattr(
                                request.app.state,
                                "quality_judge",
                                None,
                            ),
                            pricing=pricing,
                        )
                    except Exception as exc:
                        log.warning(
                            "anthropic_oauth_quality_failed",
                            error=str(exc),
                        )
                    # Brain layer (bloat findings + scorecard) and shadow
                    # trials — passthrough traffic is most of the gateway's
                    # volume; excluding it made the trust score a 46-sample
                    # sliver. Waste figures stay NOTIONAL (estimated at list
                    # price); real cost accounting is untouched.
                    try:
                        from app.scorecard import process_brain as _process_brain

                        await _process_brain(
                            pool,
                            pricing,
                            decision_id=decision_id,
                            actual_model=meta.get("actual_model"),
                        )
                    except Exception as exc:
                        log.warning(
                            "anthropic_oauth_brain_failed",
                            error=str(exc) or repr(exc),
                            error_type=type(exc).__name__,
                        )
                    # NAUTGATE-1: drift detection was dark on OAuth traffic —
                    # zero model_baselines rows despite it being most of volume.
                    # Additive + fire-and-forget; keyed on (provider, model) like
                    # the scorecard, using the session_id set in PRECAPTURE.
                    try:
                        from app.drift_engine import process_drift as _process_drift

                        await _process_drift(pool, decision_id=decision_id)
                    except Exception as exc:
                        log.warning(
                            "anthropic_oauth_drift_failed",
                            error=str(exc) or repr(exc),
                            error_type=type(exc).__name__,
                        )
                    try:
                        from app.shadow import process_shadow as _process_shadow

                        await _process_shadow(
                            pool,
                            decision_id=decision_id,
                            shadow_client=getattr(request.app.state, "quality_judge", None),
                            pricing=pricing,
                        )
                    except Exception as exc:
                        log.warning(
                            "anthropic_oauth_shadow_failed",
                            error=str(exc) or repr(exc),
                            error_type=type(exc).__name__,
                        )
                    # Engram-OSS / SecondBrain memory ingest — byte-by-byte
                    # parity with flow-memory-proxy's storeDelta:
                    #   - agent_id constant "claude-code" (matches proxy.js:138)
                    #   - session_id read from payload._session_id with the
                    #     "proxy" fallback (matches proxy.js:283)
                    #   - same writes to agents_memory.memories on stargate
                    # Fire-and-forget; the live config flag
                    # (nautgate.app_config.sb_ingest.enabled) gates whether
                    # writes actually happen. When false (current default
                    # while flow-memory-proxy still runs), this is a no-op.
                    try:
                        from app.sb_memory import ingest_outcome as _sb_ingest

                        sb_session_id = "proxy"
                        if isinstance(payload, dict):
                            sid = payload.get("_session_id")
                            if isinstance(sid, str) and sid.strip():
                                sb_session_id = sid
                        await _sb_ingest(
                            app_pool=pool,
                            agent_id="claude-code",
                            session_id=sb_session_id,
                            model=meta.get("actual_model") or requested_model,
                            prompt_body=(captured_body.body if captured_body else None),
                            response_body=response_captured.body,
                        )
                    except Exception as exc:
                        log.warning(
                            "anthropic_oauth_engram_failed",
                            error=str(exc),
                        )
                except Exception as exc:
                    log.warning(
                        "anthropic_oauth_outcome_failed",
                        error=str(exc) or repr(exc),
                        error_type=type(exc).__name__,
                    )

    return StreamingResponse(
        _relay(),
        status_code=upstream_status,
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
