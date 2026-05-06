import time
import uuid

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import auth_stub
from app.db import queries

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
    agent_id = await auth_stub(request)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    if payload.get("stream"):
        return _stub(coming_in="day-3", message="Streaming SSE lands Day 3 (Tech Paper §11).")

    model_requested = payload.get("model")
    if not model_requested:
        raise HTTPException(status_code=400, detail="missing 'model'")

    nautrouter = getattr(request.app.state, "nautrouter", None)
    if nautrouter is None:
        raise HTTPException(status_code=503, detail="nautrouter_unavailable")

    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")

    decision_id = uuid.uuid4()
    started = time.monotonic()
    prompt_excerpt = queries.excerpt_last_user_message(payload.get("messages"))

    # PRECAPTURE — synchronous audit row before forwarding upstream (Tech Paper §9).
    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format="openai_chat",
        model_requested=model_requested,
        classified_tier="UNCLASSIFIED",  # Day 4 fills with real classifier output
        decision_provider="passthrough",  # Day 5 fills with real provider after SCORE+DECIDE
        decision_model=model_requested,
        decision_reason="day-2-passthrough",
        prompt_excerpt=prompt_excerpt,
    )

    # Forward upstream.
    upstream_status = 200
    upstream_resp: dict | None = None
    try:
        upstream_resp = await nautrouter.chat_completions(payload)
    except httpx.HTTPStatusError as exc:
        upstream_status = exc.response.status_code
        log.warning("nautrouter_status_error", status=upstream_status, decision_id=str(decision_id))
    except Exception as exc:
        upstream_status = 502
        log.error("nautrouter_call_failed", error=str(exc), decision_id=str(decision_id))

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
