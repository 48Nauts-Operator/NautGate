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

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.capture import capture_prompt, capture_response, capture_tools
from app.db import queries

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
            # Write outcome (fire-and-forget — don't break the response if it fails).
            if pool is not None:
                import time as _time
                duration_ms = (
                    int((_time.monotonic_ns() - started_at_ns) / 1_000_000)
                    if started_at_ns else 0
                )
                try:
                    # Best-effort body capture for the audit detail. SSE chunks
                    # accumulated above; for non-stream it's just the JSON.
                    response_text = body_buf.decode("utf-8", errors="replace")
                    response_captured = capture_response(response_text, "none")
                    from app.outcome import persist_outcome
                    spool = getattr(request.app.state, "outcome_spool", None)
                    await persist_outcome(
                        pool, spool,
                        decision_id=decision_id,
                        status_code=upstream.status_code,
                        duration_ms=duration_ms,
                        response_body=response_captured.body,
                        response_body_truncated_at_byte=response_captured.truncated_at_byte,
                        response_size_bytes=len(body_buf),
                        actual_provider="chatgpt-oauth",
                        actual_model="codex-subscription",
                    )
                except Exception as exc:
                    log.warning("oauth_outcome_persist_failed", error=str(exc))

    return StreamingResponse(
        _relay(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
