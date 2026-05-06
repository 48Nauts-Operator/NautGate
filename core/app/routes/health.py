import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "reason": "db_pool_unavailable"},
        )
    try:
        async with asyncio.timeout(0.5):
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
    except (TimeoutError, Exception) as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "reason": "db_unreachable", "error": str(exc)},
        )

    nautrouter = getattr(request.app.state, "nautrouter", None)
    nautrouter_ready: bool | None = None
    if nautrouter is not None:
        try:
            nautrouter_ready = await nautrouter.health()
        except Exception:
            nautrouter_ready = False

    return {"status": "ok", "db": True, "nautrouter": nautrouter_ready}
