"""V1 routes. /v1/chat/completions, /v1/messages share a common pipeline core.

Pipeline (Tech Paper §2):
    auth → CLASSIFY → SCORE → DECIDE → PRECAPTURE → forward → outcome → respond

Both /v1/chat/completions and /v1/messages translate to the canonical OpenAI Chat
shape before forwarding to NautRouter, then translate the response back as needed.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.auth import authenticate
from app.capture import capture_prompt, capture_response
from app.classify import assemble_user_text, classify
from app.db import queries
from app.formats import anthropic as ant
from app.formats import openai_responses as resp_fmt
from app.outcome import persist_outcome
from app.provider_health import upsert_health
from app.scoring import resolve_healthy, score, to_tier
from app.streaming import ACCUMULATOR_CAP_BYTES_DEFAULT, StreamCapture, parse_sse_for_outcome

AUTO_MODEL_TOKEN = "auto"

router = APIRouter(prefix="/v1", tags=["v1"])
log = structlog.get_logger()


def _stub(coming_in: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"error": "not_implemented", "message": message},
        headers={"X-Nautgate-Coming-In": coming_in},
    )


# ============================================================================
# Shared pipeline core
# ============================================================================


async def _process_chat_request(
    request: Request,
    *,
    payload: dict,
    inbound_format: str,
    response_translator: Callable[[dict, str], dict] | None = None,
    stream_translator: Callable[[bytes], list[bytes]] | None = None,
    stream_translator_finish: Callable[[], list[bytes]] | None = None,
) -> Response:
    """Run the full chat pipeline against a canonical OpenAI-Chat-shaped payload.

    Args:
        request: FastAPI request (for app.state and request.state).
        payload: OpenAI Chat-shaped dict. Mutated only to rewrite `model` for auto-routing.
        inbound_format: identifier written to route_decisions.inbound_format
            (e.g. "openai_chat", "anthropic").
        response_translator: optional fn(upstream_dict, decision_model) → final dict
            applied to the non-streaming upstream response before JSON-encoding.
        stream_translator: optional fn(chunk_bytes) → list[bytes] applied to each
            upstream SSE chunk on the streaming path. The client sees translator output.
        stream_translator_finish: optional fn() → list[bytes] flushed at stream close.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")

    agent_id = await authenticate(pool, request)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    model_requested = payload.get("model")
    if not model_requested:
        raise HTTPException(status_code=400, detail="missing 'model'")

    nautrouter = getattr(request.app.state, "nautrouter", None)
    if nautrouter is None:
        raise HTTPException(status_code=503, detail="nautrouter_unavailable")

    decision_id = uuid.uuid4()
    started = time.monotonic()
    messages = payload.get("messages")
    prompt_excerpt = queries.excerpt_last_user_message(messages)

    # CLASSIFY
    classification = classify(assemble_user_text(messages))

    # SCORE
    score_vector = score(payload)
    tier = to_tier(score_vector)

    # DECIDE
    routing_table = getattr(request.app.state, "routing_table", None)
    health_tracker = getattr(request.app.state, "health_tracker", None)
    if model_requested == AUTO_MODEL_TOKEN:
        if routing_table is None:
            raise HTTPException(status_code=503, detail="routing_table_unavailable")
        is_unhealthy = health_tracker.is_unhealthy if health_tracker else (lambda *_: False)
        route_pick = resolve_healthy(tier, routing_table, is_unhealthy)
        decision_provider = route_pick.provider
        decision_model = route_pick.model
        decision_reason = f"auto:{tier}->{route_pick.provider}/{route_pick.model}"
        payload["model"] = decision_model
    else:
        decision_provider = "passthrough"
        decision_model = model_requested
        decision_reason = f"explicit:{model_requested}"

    captured = capture_prompt(messages, classification.sensitivity)

    # PRECAPTURE
    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format=inbound_format,
        model_requested=model_requested,
        classified_tier=tier,
        classified_score=score_vector.aggregate,
        classified_sensitivity=classification.sensitivity,
        classified_signals=classification.signals,
        decision_provider=decision_provider,
        decision_model=decision_model,
        decision_reason=decision_reason,
        prompt_excerpt=prompt_excerpt,
        prompt_body=captured.body,
        prompt_body_truncated_at_byte=captured.truncated_at_byte,
    )

    request.state.classified_sensitivity = classification.sensitivity
    request.state.decision_provider = decision_provider
    request.state.decision_model = decision_model

    common_headers = {
        "X-Nautgate-Decision-Id": str(decision_id),
        "X-Nautgate-Provider": decision_provider,
        "X-Nautgate-Model": str(decision_model),
        "X-Nautgate-Tier": tier,
        "X-Nautgate-Score": f"{score_vector.aggregate:.4f}",
        "X-Nautgate-Brain-Used": "false",
        "X-Nautgate-Inbound-Format": inbound_format,
    }

    if payload.get("stream"):
        return _streaming_response(
            request=request,
            payload=payload,
            decision_id=decision_id,
            started=started,
            decision_provider=decision_provider,
            decision_model=decision_model,
            classification_sensitivity=classification.sensitivity,
            health_tracker=health_tracker,
            pool=pool,
            nautrouter=nautrouter,
            common_headers=common_headers,
            stream_translator=stream_translator,
            stream_translator_finish=stream_translator_finish,
        )

    # --- non-streaming ---
    upstream_status = 200
    upstream_resp: dict | None = None
    try:
        raw = await nautrouter.chat_completions(payload)
    except httpx.HTTPStatusError as exc:
        upstream_status = exc.response.status_code
        log.warning("nautrouter_status_error", status=upstream_status, decision_id=str(decision_id))
        raw = None
    except Exception as exc:
        upstream_status = 502
        log.error("nautrouter_call_failed", error=str(exc), decision_id=str(decision_id))
        raw = None

    if isinstance(raw, dict):
        upstream_resp = raw
    elif raw is not None:
        upstream_status = 502
        log.warning(
            "nautrouter_non_dict_response",
            response_type=type(raw).__name__,
            decision_id=str(decision_id),
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    was_empty = False
    if upstream_resp:
        usage = upstream_resp.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        try:
            content = upstream_resp["choices"][0]["message"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        was_empty = bool((completion_tokens or 0) > 0 and not content)

    response_captured = capture_response(upstream_resp, classification.sensitivity)
    spool = getattr(request.app.state, "outcome_spool", None)
    await persist_outcome(
        pool,
        spool,
        decision_id=decision_id,
        status_code=upstream_status,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        was_empty=was_empty,
        response_body=response_captured.body,
        response_body_truncated_at_byte=response_captured.truncated_at_byte,
    )

    if decision_provider != "passthrough":
        if health_tracker is not None:
            health_tracker.record(decision_provider, decision_model, was_empty=was_empty)
        try:
            await upsert_health(
                pool,
                provider=decision_provider,
                model=decision_model,
                duration_ms=duration_ms,
                was_empty=was_empty,
                success=200 <= upstream_status < 300,
            )
        except Exception as exc:
            log.warning(
                "provider_health_upsert_failed",
                error=str(exc),
                provider=decision_provider,
                model=decision_model,
            )

    if upstream_resp is None:
        raise HTTPException(status_code=502, detail="upstream_failed")

    final = (
        response_translator(upstream_resp, decision_model) if response_translator else upstream_resp
    )

    headers = dict(common_headers)
    headers["X-Nautgate-Latency-Ms"] = str(duration_ms)
    headers["X-Nautgate-Was-Empty"] = "true" if was_empty else "false"
    return JSONResponse(content=final, headers=headers)


def _streaming_response(
    *,
    request: Request,
    payload: dict,
    decision_id: uuid.UUID,
    started: float,
    decision_provider: str,
    decision_model: str,
    classification_sensitivity: str,
    health_tracker,
    pool,
    nautrouter,
    common_headers: dict,
    stream_translator: Callable[[bytes], list[bytes]] | None,
    stream_translator_finish: Callable[[], list[bytes]] | None,
) -> StreamingResponse:
    """Tee + cap + (optional) inline format translation. Tech Paper §11."""
    capture = StreamCapture(cap_bytes=ACCUMULATOR_CAP_BYTES_DEFAULT)
    state = {"first_byte_ms": None, "client_disconnected": False}

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in nautrouter.chat_completions_stream(payload):
                if state["first_byte_ms"] is None:
                    state["first_byte_ms"] = int((time.monotonic() - started) * 1000)
                # Capture the upstream (canonical) bytes for parsing/audit.
                capture.append(chunk)
                # If a translator is active, what the client sees is the translated form.
                if stream_translator is None:
                    yield chunk
                else:
                    for out in stream_translator(chunk):
                        yield out
            # Upstream finished cleanly. If a translator buffered partial state, flush.
            if stream_translator_finish is not None:
                for out in stream_translator_finish():
                    yield out
        except asyncio.CancelledError:
            state["client_disconnected"] = True
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            parsed = parse_sse_for_outcome(bytes(capture.accumulator))
            response_captured = capture_response(
                parsed.get("assembled_content"), classification_sensitivity
            )
            spool = getattr(request.app.state, "outcome_spool", None)
            try:
                await persist_outcome(
                    pool,
                    spool,
                    decision_id=decision_id,
                    status_code=200,
                    duration_ms=duration_ms,
                    first_byte_ms=state["first_byte_ms"],
                    prompt_tokens=parsed.get("prompt_tokens"),
                    completion_tokens=parsed.get("completion_tokens"),
                    reasoning_tokens=parsed.get("reasoning_tokens"),
                    was_empty=parsed.get("was_empty", False),
                    was_truncated=capture.was_truncated,
                    truncated_at_byte=capture.truncated_at_byte,
                    client_disconnected=state["client_disconnected"],
                    response_body=response_captured.body,
                    response_body_truncated_at_byte=response_captured.truncated_at_byte,
                )
            except Exception as exc:
                log.error(
                    "outcome_write_failed_in_stream",
                    error=str(exc),
                    decision_id=str(decision_id),
                )

            if decision_provider != "passthrough":
                if health_tracker is not None:
                    health_tracker.record(
                        decision_provider, decision_model, was_empty=parsed.get("was_empty", False)
                    )
                try:
                    await upsert_health(
                        pool,
                        provider=decision_provider,
                        model=decision_model,
                        duration_ms=duration_ms,
                        was_empty=parsed.get("was_empty", False),
                        success=not state["client_disconnected"],
                    )
                except Exception as exc:
                    log.warning("provider_health_upsert_failed_stream", error=str(exc))

    headers = dict(common_headers)
    headers.update(
        {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ============================================================================
# OpenAI Chat
# ============================================================================


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    return await _process_chat_request(request, payload=payload, inbound_format="openai_chat")


# ============================================================================
# Anthropic Messages (Week 1b)
# ============================================================================


@router.post("/messages")
async def messages(request: Request) -> Response:
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    payload = ant.request_to_openai_chat(raw)

    stream_translator = None
    stream_translator_finish = None
    if payload.get("stream"):
        # The translator needs the chosen model name; we don't know it yet (auto-routing
        # may rewrite it). Build a thin closure that defers binding until the first chunk.
        translator_holder: dict = {"t": None}

        def _ensure(model_name: str):
            if translator_holder["t"] is None:
                translator_holder["t"] = ant.AnthropicStreamTranslator(model=model_name)
            return translator_holder["t"]

        def _on_chunk(chunk: bytes) -> list[bytes]:
            t = _ensure(getattr(request.state, "decision_model", payload.get("model", "")))
            return t.feed(chunk)

        def _on_finish() -> list[bytes]:
            t = translator_holder["t"]
            return t.finish() if t else []

        stream_translator = _on_chunk
        stream_translator_finish = _on_finish

    return await _process_chat_request(
        request,
        payload=payload,
        inbound_format="anthropic",
        response_translator=ant.response_to_anthropic,
        stream_translator=stream_translator,
        stream_translator_finish=stream_translator_finish,
    )


# ============================================================================
# Stubs (filled later)
# ============================================================================


@router.post("/responses")
async def responses(request: Request) -> Response:
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    payload = resp_fmt.request_to_openai_chat(raw)

    # Streaming for /v1/responses lands as a follow-up; the event set
    # (response.created / output_item / content_part / output_text.delta /
    #  output_item.done / response.completed) is large enough to deserve its own commit.
    if payload.get("stream"):
        return _stub(
            coming_in="week-1b-stream",
            message="Streaming /v1/responses lands in a follow-up — non-streaming works today.",
        )

    return await _process_chat_request(
        request,
        payload=payload,
        inbound_format="openai_responses",
        response_translator=resp_fmt.response_to_openai_responses,
    )


@router.get("/models")
async def list_models(request: Request) -> Response:
    """Lists models available via NautGate.

    Composed from the routing table (each tier's primary + fallback) plus the
    synthetic ``auto`` entry. Annotates `nautgate_unhealthy: true` for any
    (provider, model) the in-process health tracker has demoted.

    Shape mirrors OpenAI's `/v1/models`: `{object, data: [{id, object, owned_by, ...}]}`.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    table = getattr(request.app.state, "routing_table", None) or {}
    tracker = getattr(request.app.state, "health_tracker", None)

    seen: dict[tuple[str, str], dict] = {}
    tier_seen: dict[tuple[str, str], list[str]] = {}
    for tier_name, body in table.items():
        for slot in ("primary", "fallback"):
            entry = body.get(slot) if isinstance(body, dict) else None
            if not isinstance(entry, dict):
                continue
            provider = entry.get("provider")
            model = entry.get("model")
            if not provider or not model:
                continue
            key = (provider, model)
            tier_seen.setdefault(key, []).append(f"{tier_name}:{slot}")
            if key not in seen:
                seen[key] = {
                    "id": model,
                    "object": "model",
                    "owned_by": provider,
                    "nautgate_provider": provider,
                    "nautgate_tiers": [],
                }

    for key, item in seen.items():
        item["nautgate_tiers"] = tier_seen.get(key, [])
        if tracker is not None and tracker.is_unhealthy(*key):
            item["nautgate_unhealthy"] = True

    data = sorted(seen.values(), key=lambda m: (m["nautgate_provider"], m["id"]))
    # Synthetic "auto" entry — NautGate's tier-driven router.
    data.insert(
        0,
        {
            "id": "auto",
            "object": "model",
            "owned_by": "nautgate",
            "nautgate_provider": "nautgate",
            "nautgate_tiers": list(table.keys()),
        },
    )
    return JSONResponse({"object": "list", "data": data})


@router.get("/stats")
async def stats(request: Request) -> Response:
    """Aggregate stats for the authenticated agent over the last `hours` (default 24).

    Reads route_decisions + route_outcomes; all counts default to 0 when empty.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)

    try:
        hours = int(request.query_params.get("hours", "24"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    body = await queries.get_stats(pool, agent_id=agent_id, hours=hours)
    return JSONResponse(body)


@router.get("/profile")
async def get_profile(request: Request) -> JSONResponse:
    return _stub(coming_in="week-1", message="Profile endpoint lands later in Week 1.")
