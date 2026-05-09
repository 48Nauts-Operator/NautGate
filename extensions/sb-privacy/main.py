"""sb-privacy — privacy-safe routing + tamper-evident audit for sensitive prompts.

Subscribes to:
  - before_route: when classified_sensitivity ∈ {pii, secret}, returns
    `preferred_models` (the privacy-safe allowlist) and `demoted_models`
    (anything NOT on the allowlist that would otherwise be picked).
  - on_request: writes a hash-chained row to nautgate.privacy_log.

Configuration:
  SB_PRIVACY_DB_URL          — required asyncpg DSN (defaults to nautgate's DB)
  SB_PRIVACY_ALLOWLIST_PATH  — YAML file listing privacy-safe (provider, model) pairs.
                                Defaults to ./allowlist.yaml in the package dir.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from chain import GENESIS_HASH, hash_payload, link_hash
from migrate import apply as apply_migration

logging.basicConfig(level=os.environ.get("SB_PRIVACY_LOG_LEVEL", "INFO"))
log = logging.getLogger("sb-privacy")

DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "allowlist.yaml"

SENSITIVE_LEVELS = {"pii", "secret"}


def _load_allowlist(path: Path | str) -> dict:
    """Returns {"providers": [...], "models": [...]} from YAML, or empty defaults."""
    p = Path(path)
    if not p.exists():
        return {"providers": [], "models": []}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("allowlist_parse_failed path=%s err=%s", path, exc)
        return {"providers": [], "models": []}
    return {
        "providers": list(raw.get("providers") or []),
        "models": list(raw.get("models") or []),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("SB_PRIVACY_DB_URL")
    app.state.pool = None
    app.state.allowlist = _load_allowlist(
        os.environ.get("SB_PRIVACY_ALLOWLIST_PATH") or DEFAULT_ALLOWLIST
    )
    app.state.chain_lock = asyncio.Lock()

    if dsn:
        try:
            app.state.pool = await asyncpg.create_pool(
                dsn=dsn, min_size=1, max_size=4, command_timeout=5.0
            )
            await apply_migration(app.state.pool)
            log.info("sb_privacy_db_connected")
        except Exception as exc:
            log.error("sb_privacy_db_connect_failed err=%s", exc)
            app.state.pool = None
    else:
        log.warning("sb_privacy_no_db_url — chain writes will be skipped")

    try:
        yield
    finally:
        if app.state.pool is not None:
            await app.state.pool.close()


app = FastAPI(title="sb-privacy", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "db_connected": app.state.pool is not None,
            "allowlist_models": len(app.state.allowlist["models"]),
            "allowlist_providers": len(app.state.allowlist["providers"]),
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
    """Synchronous hint endpoint. Returns privacy-safe routing hints for
    sensitive prompts; otherwise empty dict (no influence on routing).
    """
    body = await _read_json(request)
    sensitivity = body.get("classified_sensitivity")
    if sensitivity not in SENSITIVE_LEVELS:
        return JSONResponse({})

    allow = app.state.allowlist
    return JSONResponse(
        {
            "promoted_models": list(allow["models"]),
            "brain_hints": {
                "reason": f"sensitivity={sensitivity}; routed via privacy allowlist",
                "allowlist_providers": list(allow["providers"]),
            },
        }
    )


@app.post("/v1/on_request")
async def on_request(request: Request) -> Response:
    """Append a hash-chained row to privacy_log for sensitive prompts."""
    body = await _read_json(request)
    sensitivity = body.get("classified_sensitivity")
    if sensitivity not in SENSITIVE_LEVELS:
        return JSONResponse({"ok": True, "logged": False})

    if app.state.pool is None:
        return JSONResponse({"ok": True, "logged": False, "reason": "no_db"})

    decision_id = body.get("decision_id")
    agent_id = body.get("agent_id") or "unknown"
    payload_text = body.get("prompt_excerpt") or body.get("prompt_body") or ""
    payload_hash = hash_payload(str(payload_text))
    ts = datetime.now(UTC)

    async with app.state.chain_lock:
        async with app.state.pool.acquire() as conn:
            prev = await conn.fetchval(
                "SELECT this_hash FROM nautgate.privacy_log ORDER BY id DESC LIMIT 1"
            )
            prev_hash = prev or GENESIS_HASH
            this_hash = link_hash(
                prev_hash=prev_hash,
                payload_hash=payload_hash,
                ts=ts,
                decision_id=decision_id,
                agent_id=agent_id,
                sensitivity=sensitivity,
            )
            try:
                await conn.execute(
                    """
                    INSERT INTO nautgate.privacy_log
                        (ts, decision_id, agent_id, sensitivity, signals,
                         payload_hash, prev_hash, this_hash)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                    """,
                    ts,
                    decision_id,
                    agent_id,
                    sensitivity,
                    _signals_json(body.get("classified_signals")),
                    payload_hash,
                    prev_hash,
                    this_hash,
                )
            except Exception as exc:
                log.warning("privacy_log_insert_failed err=%s", exc)
                return JSONResponse({"ok": True, "logged": False, "error": str(exc)})

    return JSONResponse({"ok": True, "logged": True, "this_hash": this_hash})


def _signals_json(signals) -> str | None:
    import json as _json

    if not signals:
        return None
    try:
        return _json.dumps(signals)
    except (TypeError, ValueError):
        return None
