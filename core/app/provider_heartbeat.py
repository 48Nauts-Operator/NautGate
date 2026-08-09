"""Active provider heartbeat — a tiny liveness ping per provider every ~60s.

Secondary to the passive signal (real-traffic status codes in route_outcomes,
which reflect the *actual* subscription capacity pool). The heartbeat fills the
"no recent traffic" gap and gives a live reachability + latency dot. An Anthropic
*metered* ping hits a different pool than the OAuth subscription, so the passive
signal stays primary for the Anthropic badge.

Results live in ``app.state.provider_status`` (in-memory) — no table needed for a
live badge. Reuses the probe transports + canary runner.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from app.drift_investigator import Canary, _run_canary, _select_transports

log = structlog.get_logger()

# Cheapest tiny probe — one token, deterministic.
_PING = Canary(
    name="heartbeat", suite="heartbeat", prompt="Reply with: OK", max_tokens=1, temperature=0.0
)

# (label, provider, model, require_via) — providers on the status strip.
# require_via pins which transport leg counts for this badge: Codex must use the
# OAuth subscription (not a metered OpenAI fallback, which is a different product).
DEFAULT_TARGETS: tuple[tuple[str, str, str, str | None], ...] = (
    ("anthropic-oauth", "anthropic", "claude-haiku-4-5", None),
    ("openrouter", "openrouter", "openrouter/openai/gpt-4o-mini", None),
    ("chatgpt-oauth", "openai", "gpt-5-codex", "chatgpt-oauth"),
)


async def ping_once(
    client: httpx.AsyncClient, provider: str, model: str, pricing, require_via: str | None = None
) -> dict:
    """Fire one tiny ping at the cheapest available transport. Classifies:
    ok (2xx) / degraded (429|529) / down (other error). When require_via is set
    and no matching transport exists (e.g. no Codex OAuth token), returns no-cred."""
    transports = _select_transports(provider, model, prefer_oauth=True)
    if require_via:
        transports = [t for t in transports if t.via == require_via]
    if not transports:
        return {"status": "no-cred", "status_code": None, "latency_ms": None, "via": None}
    r = await _run_canary(client, _PING, provider, model, transports[0], pricing)
    # Classify by status code FIRST — a 4xx also populates r.error with the
    # body text, so checking r.error first would misfile everything as down.
    if r.status_code in (429, 529):
        status = "degraded"
    elif r.status_code and 200 <= r.status_code < 300:
        status = "ok"
    elif r.status_code in (400, 401, 403):
        # A fixed 1-token ping that 4xxs is OUR credential/billing problem
        # (expired key, no credits), not a provider outage. Surfacing it as
        # "down" made the badge lie whenever traffic went quiet.
        status = "no-cred"
    else:
        status = "down"
    # No usable first-party credential for a Claude target → ping the same
    # model through OpenRouter instead. Measures Claude availability via a
    # reseller rather than api.anthropic.com, marked in `via` for honesty.
    if (
        status == "no-cred"
        and provider in ("anthropic", "passthrough")
        and "claude" in model.lower()
    ):
        from app.shadow import openrouter_claude_id

        alt = openrouter_claude_id(model)
        alt_transports = _select_transports("openrouter", alt, prefer_oauth=False)
        if alt_transports:
            r2 = await _run_canary(client, _PING, "openrouter", alt, alt_transports[0], pricing)
            if r2.status_code and 200 <= r2.status_code < 300:
                return {
                    "status": "ok",
                    "status_code": r2.status_code,
                    "latency_ms": r2.first_byte_ms or r2.duration_ms,
                    "via": "openrouter-fallback",
                }
    return {
        "status": status,
        "status_code": r.status_code,
        "latency_ms": r.first_byte_ms or r.duration_ms,
        "via": r.via,
    }


async def run_scheduler(pool, *, pricing, state, tick_seconds: int = 60) -> None:
    """Populate ``state.provider_status[label]`` every tick. Never raises."""
    log.info("provider_heartbeat_started", tick_seconds=tick_seconds)
    import time as _time

    from app.app_config import is_offline

    while True:
        try:
            # Offline / air-gapped: stand down. This loop is the main source of
            # unsolicited outbound traffic — it pings providers on a timer even
            # when every request is being served locally.
            if await is_offline(pool):
                state.clear()
                await asyncio.sleep(tick_seconds)
                continue
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0), http2=False
            ) as client:
                for label, provider, model, require_via in DEFAULT_TARGETS:
                    try:
                        res = await ping_once(
                            client, provider, model, pricing, require_via=require_via
                        )
                    except Exception as exc:
                        res = {
                            "status": "down",
                            "status_code": None,
                            "latency_ms": None,
                            "via": None,
                            "error": str(exc),
                        }
                    res["checked_at"] = _time.time()
                    state[label] = res
        except asyncio.CancelledError:
            log.info("provider_heartbeat_cancelled")
            raise
        except Exception as exc:
            log.error(
                "provider_heartbeat_iteration_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(tick_seconds)
