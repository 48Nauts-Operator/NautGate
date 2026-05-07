"""sb-capture — NautGate reference extension.

Receives the three fire-and-forget hooks (on_request, on_response, on_outcome)
and appends each payload as one NDJSON line to a configurable output file.
This is the simplest extension that demonstrates the plugin contract end-to-end.

In production, sb-capture would write to a real sink (Postgres "memories" schema,
S3, etc.). The NDJSON file is a stand-in that's trivially consumable by jq /
DuckDB / pandas for analysis.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

DEFAULT_OUTPUT_PATH = "/var/lib/sb-capture/events.ndjson"

app = FastAPI(title="sb-capture", version="0.1.0")


def _output_path() -> Path:
    return Path(os.environ.get("SB_CAPTURE_OUTPUT_PATH", DEFAULT_OUTPUT_PATH))


def _append(hook: str, body: dict) -> None:
    path = _output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"hook": hook, "received_at": time.time(), "payload": body}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


@app.get("/health")
async def health() -> Response:
    return JSONResponse({"status": "ok", "output_path": str(_output_path())})


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return body


@app.post("/v1/on_request")
async def on_request(request: Request) -> Response:
    body = await _read_json(request)
    _append("on_request", body)
    return JSONResponse({"ok": True})


@app.post("/v1/on_response")
async def on_response(request: Request) -> Response:
    body = await _read_json(request)
    _append("on_response", body)
    return JSONResponse({"ok": True})


@app.post("/v1/on_outcome")
async def on_outcome(request: Request) -> Response:
    body = await _read_json(request)
    _append("on_outcome", body)
    return JSONResponse({"ok": True})


# Optional: support after_route too (useful for quick "request done" telemetry).
@app.post("/v1/after_route")
async def after_route(request: Request) -> Response:
    body = await _read_json(request)
    _append("after_route", body)
    return JSONResponse({"ok": True})
