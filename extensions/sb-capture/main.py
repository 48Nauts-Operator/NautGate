"""sb-capture — NautGate reference extension.

Subscribes to on_request, on_response, on_outcome (and after_route) hooks and
fans each payload out to one or more sinks:

  SB_CAPTURE_SINK=ndjson   (default) — append NDJSON lines to a file
  SB_CAPTURE_SINK=postgres           — write to agents_memory.memories
  SB_CAPTURE_SINK=both               — both of the above

Postgres connection: SB_CAPTURE_DB_URL (asyncpg DSN). When the sink is
"postgres" or "both" but the DSN is unset, we fall back to NDJSON only and log
a warning at startup.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from sinks import NDJSONSink, PostgresSink

logging.basicConfig(level=os.environ.get("SB_CAPTURE_LOG_LEVEL", "INFO"))
log = logging.getLogger("sb-capture")

DEFAULT_OUTPUT_PATH = "/var/lib/sb-capture/events.ndjson"


def _build_sinks() -> list:
    sink_name = os.environ.get("SB_CAPTURE_SINK", "ndjson").lower()
    sinks: list = []

    if sink_name in ("ndjson", "both"):
        path = os.environ.get("SB_CAPTURE_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
        sinks.append(NDJSONSink(path))
        log.info("ndjson_sink_enabled path=%s", path)

    if sink_name in ("postgres", "both"):
        dsn = os.environ.get("SB_CAPTURE_DB_URL")
        if dsn:
            sinks.append(PostgresSink(dsn))
            log.info("postgres_sink_enabled")
        else:
            log.warning(
                "postgres_sink_requested_but_no_dsn — falling back to ndjson only"
            )
            if not sinks:
                sinks.append(NDJSONSink(DEFAULT_OUTPUT_PATH))

    if not sinks:
        log.warning("no_sinks_configured — falling back to default ndjson")
        sinks.append(NDJSONSink(DEFAULT_OUTPUT_PATH))

    return sinks


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sinks = _build_sinks()
    try:
        yield
    finally:
        for s in app.state.sinks:
            await s.close()


app = FastAPI(title="sb-capture", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> Response:
    sink_names = [type(s).__name__ for s in app.state.sinks]
    return JSONResponse({"status": "ok", "sinks": sink_names})


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return body


async def _fanout(hook: str, body: dict) -> None:
    for sink in app.state.sinks:
        try:
            await sink.write(hook, body)
        except Exception as exc:  # belt-and-suspenders; sinks already swallow
            log.warning("sink_write_failed sink=%s err=%s", type(sink).__name__, exc)


@app.post("/v1/on_request")
async def on_request(request: Request) -> Response:
    await _fanout("on_request", await _read_json(request))
    return JSONResponse({"ok": True})


@app.post("/v1/on_response")
async def on_response(request: Request) -> Response:
    await _fanout("on_response", await _read_json(request))
    return JSONResponse({"ok": True})


@app.post("/v1/on_outcome")
async def on_outcome(request: Request) -> Response:
    await _fanout("on_outcome", await _read_json(request))
    return JSONResponse({"ok": True})


@app.post("/v1/after_route")
async def after_route(request: Request) -> Response:
    await _fanout("after_route", await _read_json(request))
    return JSONResponse({"ok": True})
