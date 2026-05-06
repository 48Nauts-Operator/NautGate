import asyncio
import time
import uuid

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import authenticate
from app.classify import assemble_user_text, classify
from app.db import queries
from app.streaming import ACCUMULATOR_CAP_BYTES_DEFAULT, StreamCapture, parse_sse_for_outcome

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
    # Runs on the full text so a secret past the 200-char excerpt boundary still trips the gate.
    classification = classify(assemble_user_text(messages))

    # PRECAPTURE — synchronous audit row before forwarding upstream (Tech Paper §9).
    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format="openai_chat",
        model_requested=model_requested,
        classified_tier="UNCLASSIFIED",  # Day 5 fills with the complexity scorer's tier
        classified_sensitivity=classification.sensitivity,
        classified_signals=classification.signals,
        decision_provider="passthrough",  # Day 5 fills with real provider after SCORE+DECIDE
        decision_model=model_requested,
        decision_reason="day-2-passthrough",
        prompt_excerpt=prompt_excerpt,
    )

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

    # Outcome write — Day 2 ships synchronous-on-healthy-DB; durable-spool comes Day 4.
    await queries.write_outcome(
        pool,
        decision_id=decision_id,
        status_code=upstream_status,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        was_empty=was_empty,
    )

    if upstream_resp is None:
        raise HTTPException(status_code=502, detail="upstream_failed")

    return JSONResponse(
        content=upstream_resp,
        headers={
            "X-Nautgate-Decision-Id": str(decision_id),
            "X-Nautgate-Latency-Ms": str(duration_ms),
            "X-Nautgate-Provider": "passthrough",
            "X-Nautgate-Model": str(model_requested),
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
            try:
                await queries.write_outcome(
                    pool,
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
                )
            except Exception as exc:
                log.error(
                    "outcome_write_failed_in_stream",
                    error=str(exc),
                    decision_id=str(decision_id),
                )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "X-Nautgate-Decision-Id": str(decision_id),
            "X-Nautgate-Provider": "passthrough",
            "X-Nautgate-Model": str(model_requested),
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
