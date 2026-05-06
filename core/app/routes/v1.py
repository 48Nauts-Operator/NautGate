from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1", tags=["v1"])


def _stub(coming_in: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"error": "not_implemented", "message": message},
        headers={"X-Nautgate-Coming-In": coming_in},
    )


@router.post("/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    # Day 2 replaces this with PRECAPTURE → forward → outcome via NautRouter sidecar.
    return _stub(
        coming_in="day-2",
        message="OpenAI Chat completions land Day 2 (Build Plan §Week 1).",
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
    return _stub(
        coming_in="week-1",
        message="Provider model list lands later in Week 1.",
    )


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    return _stub(
        coming_in="week-1",
        message="Stats endpoint lands later in Week 1.",
    )


@router.get("/profile")
async def get_profile(request: Request) -> JSONResponse:
    return _stub(
        coming_in="week-1",
        message="Profile endpoint lands later in Week 1.",
    )
