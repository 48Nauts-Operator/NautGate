import asyncio
import time
import uuid

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import authenticate
from app.capture import capture_prompt, capture_response
from app.classify import assemble_user_text, classify
from app.db import queries
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


@router.post("/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """Day 2: non-streaming `/v1/chat/completions` forwards through NautRouter sidecar.

    Pipeline (Day 2 subset of the full Tech Paper §2 pipeline):
        auth → PRECAPTURE → forward → write outcome → return.
    Day 4 fills CLASSIFY before forward, Day 5 fills SCORE + DECIDE.
    Streaming branch is a Day-3 stub.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")

    agent_id = await authenticate(pool, request)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None

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

    # CLASSIFY — fast-path regex over the full assembled user text (Tech Paper §7.3).
    classification = classify(assemble_user_text(messages))

    # SCORE — 14-dimension complexity scorer (Day 5a). Always runs so we capture
    # tier/score for every request, even when the caller pinned an explicit model.
    score_vector = score(payload)
    tier = to_tier(score_vector)

    # DECIDE — when model is "auto", resolve via the routing table; otherwise passthrough.
    routing_table = getattr(request.app.state, "routing_table", None)
    health_tracker = getattr(request.app.state, "health_tracker", None)
    if model_requested == AUTO_MODEL_TOKEN:
        if routing_table is None:
            raise HTTPException(status_code=503, detail="routing_table_unavailable")
        # Day 5c: skip the primary if it's currently unhealthy (3+ consecutive empties).
        is_unhealthy = health_tracker.is_unhealthy if health_tracker else (lambda *_: False)
        route_pick = resolve_healthy(tier, routing_table, is_unhealthy)
        decision_provider = route_pick.provider
        decision_model = route_pick.model
        decision_reason = f"auto:{tier}->{route_pick.provider}/{route_pick.model}"
        # Rewrite payload model so NautRouter forwards to the chosen target.
        payload["model"] = decision_model
    else:
        decision_provider = "passthrough"
        decision_model = model_requested
        decision_reason = f"explicit:{model_requested}"

    # BODY_CAPTURE — policy-gated by sensitivity (Day 4c).
    captured = capture_prompt(messages, classification.sensitivity)

    # PRECAPTURE — synchronous audit row before forwarding upstream (Tech Paper §9).
    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format="openai_chat",
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

    # Stash sensitivity + chosen route for the streaming branch's response headers.
    request.state.classified_sensitivity = classification.sensitivity
    request.state.decision_provider = decision_provider
    request.state.decision_model = decision_model

    if payload.get("stream"):
        return await _stream_chat_completions(
            request,
            payload=payload,
            decision_id=decision_id,
            started=started,
            model_requested=model_requested,
            nautrouter=nautrouter,
            pool=pool,
        )

    # Forward upstream (non-streaming).
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

    # NautRouter returns a JSON list (per-provider error array) when all providers fail.
    # Treat that as an upstream failure rather than letting it crash downstream parsing.
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

    # Outcome metrics — was_empty per Tech Paper §8 (the Tongyi failure mode).
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

    # BODY_CAPTURE for the response, gated by the same sensitivity classification.
    response_captured = capture_response(upstream_resp, classification.sensitivity)

    # Outcome write — synchronous on healthy DB; falls back to spool when DB fails (Day 4d).
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

    # Day 5c: provider_health rollup + streak counter. Only when we actually
    # routed to a real provider (auto path), not for passthrough where we don't
    # know which underlying provider NautRouter picked.
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

    return JSONResponse(
        content=upstream_resp,
        headers={
            "X-Nautgate-Decision-Id": str(decision_id),
            "X-Nautgate-Latency-Ms": str(duration_ms),
            "X-Nautgate-Provider": decision_provider,
            "X-Nautgate-Model": str(decision_model),
            "X-Nautgate-Tier": tier,
            "X-Nautgate-Score": f"{score_vector.aggregate:.4f}",
            "X-Nautgate-Brain-Used": "false",
            "X-Nautgate-Was-Empty": "true" if was_empty else "false",
        },
    )


async def _stream_chat_completions(
    request: Request,
    *,
    payload: dict,
    decision_id: uuid.UUID,
    started: float,
    model_requested: str,
    nautrouter,
    pool,
) -> StreamingResponse:
    """Day 3: tee-pattern streaming with 8 MB cap (Tech Paper §11).

    The accumulator runs in-process; truncation cuts at SSE event boundaries
    (`\\n\\n`) so captured bytes are always parseable. The client receives the
    full byte stream regardless of cap — truncation is capture-only.
    """
    capture = StreamCapture(cap_bytes=ACCUMULATOR_CAP_BYTES_DEFAULT)
    state = {"first_byte_ms": None, "client_disconnected": False}

    async def gen():
        try:
            async for chunk in nautrouter.chat_completions_stream(payload):
                if state["first_byte_ms"] is None:
                    state["first_byte_ms"] = int((time.monotonic() - started) * 1000)
                capture.append(chunk)
                yield chunk
        except asyncio.CancelledError:
            # Client closed the connection while we were still receiving from upstream.
            state["client_disconnected"] = True
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            parsed = parse_sse_for_outcome(bytes(capture.accumulator))
            sensitivity = getattr(request.state, "classified_sensitivity", "none")
            response_captured = capture_response(parsed.get("assembled_content"), sensitivity)
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

            # Day 5c: provider_health rollup + streak counter for streaming.
            ds_provider = getattr(request.state, "decision_provider", "passthrough")
            ds_model = getattr(request.state, "decision_model", model_requested)
            tracker = getattr(request.app.state, "health_tracker", None)
            if ds_provider != "passthrough":
                if tracker is not None:
                    tracker.record(ds_provider, ds_model, was_empty=parsed.get("was_empty", False))
                try:
                    await upsert_health(
                        pool,
                        provider=ds_provider,
                        model=ds_model,
                        duration_ms=duration_ms,
                        was_empty=parsed.get("was_empty", False),
                        success=not state["client_disconnected"],
                    )
                except Exception as exc:
                    log.warning("provider_health_upsert_failed_stream", error=str(exc))

    decision_provider = getattr(request.state, "decision_provider", "passthrough")
    decision_model = getattr(request.state, "decision_model", model_requested)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "X-Nautgate-Decision-Id": str(decision_id),
            "X-Nautgate-Provider": decision_provider,
            "X-Nautgate-Model": str(decision_model),
            "X-Nautgate-Brain-Used": "false",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/messages")
async def messages(request: Request) -> JSONResponse:
    return _stub(
        coming_in="week-1b",
        message="Anthropic Messages format is Week 1b (Build Plan §Week 1b).",
    )


@router.post("/responses")
async def responses(request: Request) -> JSONResponse:
    return _stub(
        coming_in="week-1b",
        message="OpenAI Responses API format is Week 1b (Build Plan §Week 1b).",
    )


@router.get("/models")
async def list_models(request: Request) -> JSONResponse:
    return _stub(coming_in="week-1", message="Provider model list lands later in Week 1.")


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    return _stub(coming_in="week-1", message="Stats endpoint lands later in Week 1.")


@router.get("/profile")
async def get_profile(request: Request) -> JSONResponse:
    return _stub(coming_in="week-1", message="Profile endpoint lands later in Week 1.")
