"""sb-brain — NautGate routing hints from observed history.

Subscribes to before_route (synchronous, 50ms timeout) + on_outcome
(fire-and-forget, drives cache invalidation). Reads NautGate's own
provider_health and routing_preferences tables.

Configuration:
  SB_BRAIN_DB_URL  — required asyncpg DSN (defaults to in-cluster nautgate DB)
  SB_BRAIN_TIMEOUT_MS  — per-call query budget (default 50)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from brain import HintCache, compute_hints
from migrate import apply as apply_indexes

logging.basicConfig(level=os.environ.get("SB_BRAIN_LOG_LEVEL", "INFO"))
log = logging.getLogger("sb-brain")


def _timeout_s() -> float:
    return float(os.environ.get("SB_BRAIN_TIMEOUT_MS", "50")) / 1000.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("SB_BRAIN_DB_URL")
    app.state.pool = None
    app.state.cache = HintCache()

    if dsn:
        try:
            app.state.pool = await asyncpg.create_pool(
                dsn=dsn, min_size=1, max_size=4, command_timeout=5.0
            )
            await apply_indexes(app.state.pool)
            log.info("sb_brain_db_connected")
        except Exception as exc:
            log.error("sb_brain_db_connect_failed err=%s", exc)
            app.state.pool = None
    else:
        log.warning("sb_brain_no_db_url — every hint call returns empty")

    try:
        yield
    finally:
        if app.state.pool is not None:
            await app.state.pool.close()


app = FastAPI(title="sb-brain", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "db_connected": app.state.pool is not None,
            "cache_size": app.state.cache.size(),
        }
    )


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return body


@app.post("/v1/before_route")
async def before_route(request: Request) -> Response:
    """Synchronous hint endpoint. NautGate's caller imposes a 50ms budget on us.

    We also self-bound to that budget — if we time out internally, we return
    {} so NautGate's own timeout doesn't fire.
    """
    body = await _read_json(request)
    agent_id = body.get("agent_id")
    classified_tier = body.get("classified_tier") or "balanced"

    if app.state.pool is None or not agent_id:
        return JSONResponse({})

    bundle = await compute_hints(
        app.state.pool,
        agent_id=agent_id,
        classified_tier=classified_tier,
        cache=app.state.cache,
        timeout_s=_timeout_s(),
    )
    return JSONResponse(bundle.to_response())


@app.post("/v1/on_outcome")
async def on_outcome(request: Request) -> Response:
    """Cache invalidation. Per Tech Paper §12.3: when an outcome lands for an
    agent, drop that agent's hint cache so the next decision sees fresh data.
    """
    body = await _read_json(request)
    agent_id = body.get("agent_id")
    if agent_id:
        app.state.cache.invalidate(agent_id)
    return JSONResponse({"ok": True})


@app.post("/v1/on_response")
async def on_response(request: Request) -> Response:
    """Currently a no-op for v1 — routing decisions don't depend on response
    bodies. Reserved for v2 (e.g., quality-score-based routing).
    """
    await _read_json(request)
    return JSONResponse({"ok": True})


@app.post("/v1/log")
async def decision_log(request: Request) -> Response:
    """Decision mirror — no-op for v1; route_decisions in NautGate is the
    canonical audit log. Reserved if sb-brain ever needs its own decision history.
    """
    await _read_json(request)
    return JSONResponse({"ok": True})
