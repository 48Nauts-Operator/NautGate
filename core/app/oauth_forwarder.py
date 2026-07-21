"""ChatGPT OAuth passthrough — supports Codex CLI on Max/Plus subscriptions.

The OAuth-mode Codex CLI sends ``Authorization: Bearer <chatgpt-oauth-token>``
plus a ``chatgpt-account-id`` header and expects the request to land at
``chatgpt.com/backend-api/codex/responses`` (NOT api.openai.com). The
NautRouter sidecar doesn't speak this protocol — and we don't want to
inject our own ng_ token requirement here, because the OAuth bearer is
already the user's identity.

So this module is a *transparent* forwarder for that specific case:
  - skip the ng_-token auth gate
  - forward the request body + headers to chatgpt.com verbatim
  - stream the SSE response back
  - write an audit row with decision_provider=chatgpt-oauth + agent_id
    derived from the chatgpt-account-id header
  - capture body (policy-gated like everything else) for the audit detail

Matches flow-proxy's behavior so the same Codex session works through
NautGate without juggling auth modes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.anthropic_oauth_forwarder import (
    _MAX_OVERLOAD_RETRIES,
    _RETRY_BASE_S,
    _RETRY_CAP_S,
    _parse_retry_after,
)
from app.capture import capture_prompt, capture_response, capture_tools
from app.db import queries
from app.streaming import _iter_sse_events
from app.usage import NormalizedUsage, cache_prefix_hash, normalize_usage

log = structlog.get_logger()

CHATGPT_HOST = "chatgpt.com"
CHATGPT_PATH = "/backend-api/codex/responses"

# Hop-by-hop headers per RFC — never forwarded between client and upstream.
_HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",  # httpx recomputes this
    "content-encoding",  # let httpx pick / strip
}


def is_chatgpt_oauth_request(request: Request) -> bool:
    """Did Codex (or any client) send a ChatGPT OAuth request?

    Detected by the ``chatgpt-account-id`` header — Codex CLI attaches it
    automatically when logged in via ``codex login``. flow-proxy uses the
    same signal.
    """
    return bool(request.headers.get("chatgpt-account-id"))


def _build_forward_headers(request: Request) -> dict[str, str]:
    """Strip hop-by-hop headers; forward everything else (auth, account id,
    accept-encoding, user-agent, etc.) to chatgpt.com verbatim.
    """
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def _extract_codex_usage(body: bytes) -> NormalizedUsage:
    """Best-effort token + cache extraction from a Codex (Responses API) body.

    Responses streaming emits a ``response.completed`` event carrying
    ``response.usage`` with ``input_tokens`` (TOTAL), ``output_tokens`` and
    ``input_tokens_details.cached_tokens`` (a SUBSET of input). We map it onto
    the OpenAI shape so normalize_usage subtracts cached → fresh. Returns an
    empty NormalizedUsage when nothing parses.
    """
    usage_obj: dict | None = None
    try:
        for _etype, data in _iter_sse_events(body):
            if data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            resp = payload.get("response")
            cand = (resp or {}).get("usage") if isinstance(resp, dict) else payload.get("usage")
            if isinstance(cand, dict):
                usage_obj = cand  # keep last; final event has the authoritative totals
    except Exception:
        return NormalizedUsage()

    if not usage_obj:
        # Non-stream JSON envelope fallback.
        try:
            env = json.loads(body.decode("utf-8", errors="replace"))
            cand = env.get("usage") if isinstance(env, dict) else None
            if isinstance(cand, dict):
                usage_obj = cand
        except (ValueError, TypeError):
            return NormalizedUsage()
    if not usage_obj:
        return NormalizedUsage()

    details = usage_obj.get("input_tokens_details")
    mapped = {
        "prompt_tokens": usage_obj.get("input_tokens") or usage_obj.get("prompt_tokens"),
        "completion_tokens": usage_obj.get("output_tokens") or usage_obj.get("completion_tokens"),
        "prompt_tokens_details": {
            "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None
        },
    }
    return normalize_usage(mapped, provider_hint="openai")


async def forward_to_chatgpt(request: Request) -> StreamingResponse | JSONResponse:
    """Forward an OAuth Codex request to chatgpt.com and stream back the
    response. Writes a precapture row + outcome so the audit log groups
    these calls under agent_id taken from the chatgpt-account-id header.
    """
    raw_body = await request.body()
    payload: dict | None = None
    try:
        payload = json.loads(raw_body) if raw_body else None
    except (ValueError, TypeError):
        payload = None  # binary or malformed — still forward, just no body parse

    account_id = request.headers.get("chatgpt-account-id") or "unknown"
    # Stamp agent_id as "codex-<short-account-id>" so the dashboard can group
    # by Codex install but distinguish accounts if you ever have multiple.
    agent_id = f"codex-{account_id[:12]}" if account_id else "codex-oauth"
    decision_id = uuid.uuid4()
    inbound_format = "openai_responses_oauth"

    pool = getattr(request.app.state, "db", None)
    started_at_ns = None
    if pool is not None:
        import time as _time
        started_at_ns = _time.monotonic_ns()
        # Capture metadata before forwarding so a hang upstream still leaves
        # an audit trail (same pattern as the main path).
        from app.scoring import score
        score_vector = score(payload or {})
        messages = (payload or {}).get("messages") if isinstance(payload, dict) else None
        captured_body = capture_prompt(messages, "none") if messages else None
        captured_tools = (
            capture_tools(payload.get("tools"), "none")
            if isinstance(payload, dict) and payload.get("tools") else None
        )
        try:
            await queries.precapture(
                pool,
                decision_id=decision_id,
                agent_id=agent_id,
                inbound_format=inbound_format,
                model_requested=(payload or {}).get("model") if isinstance(payload, dict) else None,
                classified_tier="passthrough",
                classified_score=score_vector.aggregate,
                classified_sensitivity="none",
                decision_provider="chatgpt-oauth",
                decision_model=(payload or {}).get("model") if isinstance(payload, dict) else "codex-default",
                decision_reason="chatgpt-oauth:passthrough",
                prompt_body=captured_body.body if captured_body else None,
                prompt_body_truncated_at_byte=captured_body.truncated_at_byte if captured_body else None,
                tools_body=captured_tools.body if captured_tools else None,
                tools_body_truncated_at_byte=captured_tools.truncated_at_byte if captured_tools else None,
                source_ip=request.client.host if request.client else None,
                source_hostname=request.headers.get("x-forwarded-host"),
                messages_count=len(messages) if isinstance(messages, list) else None,
                tools_count=(
                    len(payload["tools"])
                    if isinstance(payload, dict) and isinstance(payload.get("tools"), list)
                    else None
                ),
                stream_flag=bool(isinstance(payload, dict) and payload.get("stream")),
                request_size_bytes=len(raw_body),
            )
        except Exception as exc:
            log.warning("oauth_precapture_failed", error=str(exc))

    fwd_headers = _build_forward_headers(request)
    url = f"https://{CHATGPT_HOST}{CHATGPT_PATH}"

    # Use a per-request client so we don't tie up the global httpx pool with
    # potentially-long-running SSE streams.
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), http2=False)

    overload_retries = 0
    try:
        for _attempt in range(_MAX_OVERLOAD_RETRIES + 1):
            upstream = await client.send(
                client.build_request(
                    method=request.method, url=url,
                    headers=fwd_headers, content=raw_body,
                ),
                stream=True,
            )
            if upstream.status_code not in (429, 529) or _attempt == _MAX_OVERLOAD_RETRIES:
                break
            retry_after = _parse_retry_after(upstream.headers.get("retry-after"))
            if upstream.status_code == 529:
                overload_retries += 1
            await upstream.aclose()
            delay = retry_after if retry_after is not None else min(
                _RETRY_CAP_S, _RETRY_BASE_S * (2 ** _attempt)) + 0.05 * _attempt
            log.info("chatgpt_oauth_retry", status=upstream.status_code,
                     attempt=_attempt + 1, delay_s=round(delay, 2))
            await asyncio.sleep(delay)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.error("oauth_forward_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"chatgpt_unreachable: {exc}") from None

    # Strip hop-by-hop headers from the response on the way back.
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    response_headers["X-Nautgate-Decision-Id"] = str(decision_id)
    response_headers["X-Nautgate-OAuth-Passthrough"] = "true"

    async def _relay() -> AsyncIterator[bytes]:
        body_buf = bytearray()
        first_byte_ns: int | None = None
        try:
            async for chunk in upstream.aiter_raw():
                if first_byte_ns is None and chunk:
                    import time as _time
                    first_byte_ns = _time.monotonic_ns()
                body_buf.extend(chunk)
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            await client.aclose()
            # Write outcome (fire-and-forget — don't break the response if it fails).
            if pool is not None:
                import time as _time
                duration_ms = (
                    int((_time.monotonic_ns() - started_at_ns) / 1_000_000)
                    if started_at_ns else 0
                )
                first_byte_ms = (
                    int((first_byte_ns - started_at_ns) / 1_000_000)
                    if first_byte_ns is not None and started_at_ns else None
                )
                try:
                    # Best-effort body capture for the audit detail. SSE chunks
                    # accumulated above; for non-stream it's just the JSON.
                    response_text = body_buf.decode("utf-8", errors="replace")
                    response_captured = capture_response(response_text, "none")
                    cu = _extract_codex_usage(bytes(body_buf))
                    from app.outcome import persist_outcome
                    spool = getattr(request.app.state, "outcome_spool", None)
                    await persist_outcome(
                        pool, spool,
                        decision_id=decision_id,
                        status_code=upstream.status_code,
                        duration_ms=duration_ms,
                        first_byte_ms=first_byte_ms,
                        upstream_overload_retries=overload_retries,
                        prompt_tokens=cu.prompt_tokens,
                        completion_tokens=cu.completion_tokens,
                        reasoning_tokens=cu.reasoning_tokens,
                        cache_read_tokens=cu.cache_read_tokens,
                        cache_write_tokens=cu.cache_write_tokens,
                        prefix_hash=cache_prefix_hash(payload),
                        response_body=response_captured.body,
                        response_body_truncated_at_byte=response_captured.truncated_at_byte,
                        response_size_bytes=len(body_buf),
                        actual_provider="chatgpt-oauth",
                        actual_model="codex-subscription",
                    )
                    # Quality eval — captures Codex prompts so the anti-pattern
                    # leaderboard sees them. Fire-and-forget; never blocks.
                    try:
                        from app.quality_eval import (
                            process_quality as _process_quality,
                        )
                        await _process_quality(
                            pool,
                            decision_id=decision_id,
                            judge_client=getattr(
                                request.app.state, "quality_judge", None,
                            ),
                            pricing=getattr(
                                request.app.state, "pricing", None,
                            ),
                        )
                    except Exception as exc:
                        log.warning("oauth_quality_failed", error=str(exc))
                    # Brain layer — same rationale as the Anthropic forwarder:
                    # passthrough traffic must feed the scorecard. Waste stays
                    # notional; real cost accounting untouched.
                    try:
                        from app.scorecard import process_brain as _process_brain
                        await _process_brain(
                            pool,
                            getattr(request.app.state, "pricing", None),
                            decision_id=decision_id,
                            actual_provider="chatgpt-oauth",
                            actual_model="codex-subscription",
                        )
                    except Exception as exc:
                        log.warning("oauth_brain_failed", error=str(exc))
                    # Engram-OSS / SecondBrain memory ingest — byte-by-byte
                    # parity with flow-memory-proxy's storeDelta:
                    #   - agent_id constant "codex" (matches proxy.js:138)
                    #   - session_id read from payload._session_id with the
                    #     "proxy" fallback (matches proxy.js:283)
                    #   - same writes to agents_memory.memories on stargate
                    # Fire-and-forget; the live config flag
                    # (nautgate.app_config.sb_ingest.enabled) gates whether
                    # writes actually happen. When false, this is a no-op.
                    try:
                        from app.sb_memory import ingest_outcome as _sb_ingest
                        sb_session_id = "proxy"
                        if isinstance(payload, dict):
                            sid = payload.get("_session_id")
                            if isinstance(sid, str) and sid.strip():
                                sb_session_id = sid
                        await _sb_ingest(
                            app_pool=pool,
                            agent_id="codex",
                            session_id=sb_session_id,
                            model=(payload or {}).get("model")
                                if isinstance(payload, dict) else None,
                            prompt_body=(
                                captured_body.body if captured_body else None
                            ),
                            response_body=response_captured.body,
                        )
                    except Exception as exc:
                        log.warning("oauth_engram_failed", error=str(exc))
                except Exception as exc:
                    log.warning("oauth_outcome_persist_failed", error=str(exc))

    return StreamingResponse(
        _relay(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
