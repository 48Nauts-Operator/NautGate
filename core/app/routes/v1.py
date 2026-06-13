"""V1 routes. /v1/chat/completions, /v1/messages share a common pipeline core.

Pipeline (Tech Paper §2):
    auth → CLASSIFY → SCORE → DECIDE → PRECAPTURE → forward → outcome → respond

Both /v1/chat/completions and /v1/messages translate to the canonical OpenAI Chat
shape before forwarding to NautRouter, then translate the response back as needed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.audit import build_audit
from app.audit_meta import extract as extract_meta
from app.audit_meta import extract_source
from app.auth import authenticate
from app.capture import capture_prompt, capture_response, capture_tools
from app.classify import assemble_user_text, classify
from app.classify_llm import maybe_upgrade_classification
from app.db import queries
from app.formats import anthropic as ant
from app.formats import openai_responses as resp_fmt
from app.outcome import persist_outcome
from app.provider_health import upsert_health
from app.scoring import resolve_healthy, score, to_tier
from app.streaming import ACCUMULATOR_CAP_BYTES_DEFAULT, StreamCapture, parse_sse_for_outcome
from app.usage import cache_prefix_hash, normalize_usage

AUTO_MODEL_TOKEN = "auto"
TOOL_CALL_ARG_EXCERPT_BYTES = 200


def _normalize_tool_calls(raw: list, sensitivity: str) -> list[dict] | None:
    """Coerce a list of {id, name, arguments} dicts into a JSONB-safe shape.

    Truncates each call's arguments to TOOL_CALL_ARG_EXCERPT_BYTES so storage
    stays bounded — the full body is already in response_body when policy
    permits. When sensitivity is "secret", arguments are dropped entirely;
    we keep the names so the audit can still answer "which tools fired".
    """
    if not raw:
        return None
    out: list[dict] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        # OpenAI Chat shape from non-streaming has nested .function.{name,arguments};
        # streaming-parsed shape from streaming.py is already flattened.
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
        name = (fn or tc).get("name") if fn is not None else tc.get("name")
        args = (fn or tc).get("arguments") if fn is not None else tc.get("arguments")
        if not name:
            continue
        entry = {"id": tc.get("id"), "name": name}
        if sensitivity != "secret" and isinstance(args, str) and args:
            entry["arguments"] = args[:TOOL_CALL_ARG_EXCERPT_BYTES]
            if len(args) > TOOL_CALL_ARG_EXCERPT_BYTES:
                entry["arguments_truncated"] = True
        out.append(entry)
    return out or None

router = APIRouter(prefix="/v1", tags=["v1"])
log = structlog.get_logger()


def _stub(coming_in: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={"error": "not_implemented", "message": message},
        headers={"X-Nautgate-Coming-In": coming_in},
    )


# ============================================================================
# Shared pipeline core
# ============================================================================


def _normalize_anthropic_snapshot(model: str | None) -> str:
    """Map Anthropic snapshot IDs (what Claude Code & the SDK send) to the
    family keys NautRouter's MODELS map uses.

    Examples:
      claude-opus-4-7-20251201        → claude-opus-4
      claude-opus-4-7                 → claude-opus-4
      claude-sonnet-4-6-20251101      → claude-sonnet-4
      claude-sonnet-4-5-20250929      → claude-sonnet-4
      claude-haiku-4-5                → claude-haiku-4.5
      claude-3-5-sonnet-20241022      → claude-sonnet-4   (best-effort fallback)
      gpt-4o, gemini-2.5-flash, …     → unchanged

    Returns the input unchanged when no rule matches.
    """
    if not model or not isinstance(model, str):
        return model or ""
    m = model.lower()
    # Strip the trailing date suffix if present (e.g. "-20251201").
    import re as _re
    m_base = _re.sub(r"-\d{8}$", "", m)
    # Anthropic family detection.
    if m_base.startswith("claude-opus-4"):
        return "claude-opus-4"
    if m_base.startswith("claude-sonnet-4"):
        return "claude-sonnet-4"
    if m_base.startswith("claude-haiku-4"):
        return "claude-haiku-4.5"
    # Best-effort for the older 3.x naming; map to the closest 4.x family.
    if m_base.startswith("claude-3-5-sonnet") or m_base.startswith("claude-3-7-sonnet"):
        return "claude-sonnet-4"
    if m_base.startswith("claude-3-5-haiku") or m_base.startswith("claude-3-haiku"):
        return "claude-haiku-4.5"
    if m_base.startswith("claude-3-opus"):
        return "claude-opus-4"
    return model


def _resolve_pricing_provider(
    decision_provider: str | None,
    actual_provider: str | None,
    model: str | None,
) -> str | None:
    """Pick the provider key used for pricing.compute_cost.

    `decision_provider` is "passthrough" for any explicit-model request
    (all Claude Code traffic, Codex passthroughs, etc.), so it never matches
    pricing.yaml on its own. When that happens we prefer what upstream told us
    (`actual_provider`), then fall back to a model-prefix heuristic so the cost
    table still lines up with a real provider/* key.
    """
    if decision_provider and decision_provider not in ("passthrough", "chatgpt-oauth"):
        return decision_provider
    if actual_provider and actual_provider not in ("passthrough", "chatgpt-oauth"):
        return actual_provider
    if not model:
        return decision_provider
    m = model.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("codex"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    return decision_provider


async def _process_chat_request(
    request: Request,
    *,
    payload: dict,
    inbound_format: str,
    response_translator: Callable[[dict, str], dict] | None = None,
    stream_translator: Callable[[bytes], list[bytes]] | None = None,
    stream_translator_finish: Callable[[], list[bytes]] | None = None,
) -> Response:
    """Run the full chat pipeline against a canonical OpenAI-Chat-shaped payload.

    Args:
        request: FastAPI request (for app.state and request.state).
        payload: OpenAI Chat-shaped dict. Mutated only to rewrite `model` for auto-routing.
        inbound_format: identifier written to route_decisions.inbound_format
            (e.g. "openai_chat", "anthropic").
        response_translator: optional fn(upstream_dict, decision_model) → final dict
            applied to the non-streaming upstream response before JSON-encoding.
        stream_translator: optional fn(chunk_bytes) → list[bytes] applied to each
            upstream SSE chunk on the streaming path. The client sees translator output.
        stream_translator_finish: optional fn() → list[bytes] flushed at stream close.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")

    agent_id = await authenticate(pool, request)

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

    # Audit metadata — what's actually shipping over the wire.
    audit_meta = extract_meta(payload)
    source_ip, source_hostname = extract_source(request)
    request.state.audit_meta = audit_meta

    # CLASSIFY — fast path
    user_text = assemble_user_text(messages)
    classification = classify(user_text)

    # CLASSIFY — slow path (LLM-confirm), gated on settings + fast-path returned "none".
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.nautgate_classify_llm_confirm:
        classification = await maybe_upgrade_classification(
            classification,
            text=user_text,
            nautrouter=nautrouter,
            enabled=True,
            model=settings.nautgate_classify_llm_confirm_model,
            timeout_s=settings.nautgate_classify_llm_confirm_timeout_s,
        )

    # SCORE
    score_vector = score(payload)
    tier = to_tier(score_vector)

    # PLUGINS: before_route — synchronous fan-out to extensions that subscribe.
    # On timeout (50ms default per ext) or error: skipped. Aggregated hints can
    # override model entirely or extend banned_models / nudge tier.
    plugins = getattr(request.app.state, "plugins", None)
    brain_hints: dict | None = None
    plugin_banned: list[str] = []
    plugin_demoted: list[str] = []
    plugin_override_model: str | None = None
    if plugins is not None and not plugins.is_empty and plugins.subscribers("before_route"):
        agg = await plugins.call_before_route(
            {
                "agent_id": agent_id,
                "inbound_format": inbound_format,
                "model_requested": model_requested,
                "classified_tier": tier,
                "classified_score": score_vector.aggregate,
                "classified_sensitivity": classification.sensitivity,
                "classified_signals": classification.signals,
                "prompt_excerpt": prompt_excerpt,
            }
        )
        if agg["brain_hints"]:
            brain_hints = agg["brain_hints"]
        plugin_banned = agg["banned_models"]
        plugin_demoted = agg["demoted_models"]
        plugin_override_model = agg["override_model"]
        if agg["preferred_tier"]:
            tier = agg["preferred_tier"]

    # DECIDE — precedence ladder per Tech Paper §2.5:
    #   2: X-Naut-Model header (per-request hard override)
    #   3: api_keys.override_model (per-key hard override)  — deferred for v1
    #   4: routing_preferences.preferred_models (caller soft pref)  — deferred for v1
    #   5: brain.override_model (brain hard override)
    #   6: brain.demoted_models (extends banned_models)
    #   7: brain.preferred_tier (tier nudge — already applied above)
    #   8: score-based tier pick (default)
    routing_table = getattr(request.app.state, "routing_table", None)
    health_tracker = getattr(request.app.state, "health_tracker", None)

    header_override = request.headers.get("x-naut-model")
    hard_override = header_override or plugin_override_model

    if hard_override:
        decision_provider = "override"
        decision_model = hard_override
        source = "header" if header_override else "brain"
        decision_reason = f"override:{source}->{hard_override}"
        payload["model"] = decision_model
    elif model_requested == AUTO_MODEL_TOKEN:
        if routing_table is None:
            raise HTTPException(status_code=503, detail="routing_table_unavailable")
        prefs = await queries.get_routing_preferences(pool, agent_id=agent_id)
        is_unhealthy = health_tracker.is_unhealthy if health_tracker else (lambda *_: False)

        # Brain layer: scorecard-based demotion. Pull demoted models for this
        # tier and merge into banned_models. Note in decision_reason if used.
        scorecard_demoted: list[str] = []
        scorecard_demotion_note: str | None = None
        try:
            from app.scorecard import is_demoted as _scorecard_is_demoted
            primary_pick = resolve_healthy(tier, routing_table, lambda *_: False)
            primary_demoted, primary_score = await _scorecard_is_demoted(
                pool, provider=primary_pick.provider, model=primary_pick.model, tier=tier,
            )
            if primary_demoted:
                scorecard_demoted.append(primary_pick.model)
                scorecard_demotion_note = (
                    f"scorecard_demoted:{primary_pick.model}@{tier}:{primary_score:.2f}"
                )
        except Exception as exc:
            log.warning("scorecard_lookup_failed", error=str(exc), tier=tier)

        # Banned: caller prefs + extension bans + brain demotions (level 6) + scorecard.
        all_banned = list(dict.fromkeys([
            *prefs["banned_models"], *plugin_banned, *plugin_demoted, *scorecard_demoted,
        ]))
        route_pick = resolve_healthy(tier, routing_table, is_unhealthy, banned_models=all_banned)
        decision_provider = route_pick.provider
        decision_model = route_pick.model
        decision_reason = f"auto:{tier}->{route_pick.provider}/{route_pick.model}"
        if scorecard_demotion_note:
            decision_reason += f" ({scorecard_demotion_note})"
        payload["model"] = decision_model
    else:
        decision_provider = "passthrough"
        decision_model = model_requested
        decision_reason = f"explicit:{model_requested}"
        # Normalize Anthropic snapshot IDs (what Claude Code sends, e.g.
        # "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5") to
        # NautRouter's MODELS map keys ("claude-opus-4", etc.). Without this,
        # NautRouter sees an unknown model and falls back to its default
        # routing, which was producing empty/Gemini-shaped responses for
        # Claude Code.
        normalized = _normalize_anthropic_snapshot(model_requested)
        if normalized != model_requested:
            payload["model"] = normalized
            decision_reason = f"explicit:{model_requested}->{normalized}"

    captured = capture_prompt(messages, classification.sensitivity)
    captured_tools = capture_tools(payload.get("tools"), classification.sensitivity)

    # Heuristic session id for drift detection (compaction events).
    from app.drift import compute_session_id
    session_id = compute_session_id(agent_id, messages)

    # PRECAPTURE
    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format=inbound_format,
        model_requested=model_requested,
        classified_tier=tier,
        classified_score=score_vector.aggregate,
        classified_sensitivity=classification.sensitivity,
        classified_signals=classification.signals,
        brain_hints=brain_hints,
        decision_provider=decision_provider,
        decision_model=decision_model,
        decision_reason=decision_reason,
        prompt_excerpt=prompt_excerpt,
        prompt_body=captured.body,
        prompt_body_truncated_at_byte=captured.truncated_at_byte,
        tools_body=captured_tools.body,
        tools_body_truncated_at_byte=captured_tools.truncated_at_byte,
        source_ip=source_ip,
        source_hostname=source_hostname,
        messages_count=audit_meta["messages_count"],
        tools_count=audit_meta["tools_count"],
        stream_flag=audit_meta["stream_flag"],
        request_size_bytes=audit_meta["request_size_bytes"],
        session_id=session_id,
        project_id=getattr(request.state, "project_id", None),
    )

    # PLUGINS: on_request — fire-and-forget after PRECAPTURE.
    if plugins is not None and not plugins.is_empty:
        plugins.dispatch_on_request(
            {
                "decision_id": decision_id,
                "agent_id": agent_id,
                "inbound_format": inbound_format,
                "model_requested": model_requested,
                "decision_provider": decision_provider,
                "decision_model": decision_model,
                "classified_tier": tier,
                "classified_sensitivity": classification.sensitivity,
                "prompt_excerpt": prompt_excerpt,
                "prompt_body": captured.body,
            }
        )

    request.state.classified_sensitivity = classification.sensitivity
    request.state.decision_provider = decision_provider
    request.state.decision_model = decision_model
    request.state.plugins = plugins

    common_headers = {
        "X-Nautgate-Decision-Id": str(decision_id),
        "X-Nautgate-Provider": decision_provider,
        "X-Nautgate-Model": str(decision_model),
        "X-Nautgate-Tier": tier,
        "X-Nautgate-Score": f"{score_vector.aggregate:.4f}",
        "X-Nautgate-Brain-Used": "false",
        "X-Nautgate-Inbound-Format": inbound_format,
    }

    if payload.get("stream"):
        return _streaming_response(
            request=request,
            payload=payload,
            decision_id=decision_id,
            started=started,
            decision_provider=decision_provider,
            decision_model=decision_model,
            classification_sensitivity=classification.sensitivity,
            health_tracker=health_tracker,
            pool=pool,
            nautrouter=nautrouter,
            common_headers=common_headers,
            stream_translator=stream_translator,
            stream_translator_finish=stream_translator_finish,
            agent_id=agent_id,
            session_id=session_id,
            captured_prompt_body=captured.body,
        )

    # --- non-streaming ---
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

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    was_empty = False
    tool_calls_made: list[dict] | None = None
    prompt_tokens = completion_tokens = None
    reasoning_tokens = cache_read_tokens = cache_write_tokens = None
    if upstream_resp:
        _usage = upstream_resp.get("usage") or {}
        _n = normalize_usage(_usage, provider_hint=decision_provider)
        prompt_tokens = _n.prompt_tokens
        completion_tokens = _n.completion_tokens
        reasoning_tokens = _n.reasoning_tokens
        cache_read_tokens = _n.cache_read_tokens
        cache_write_tokens = _n.cache_write_tokens
        try:
            msg = upstream_resp["choices"][0]["message"]
            content = msg.get("content", "") or ""
            raw_calls = msg.get("tool_calls") or []
        except (KeyError, IndexError, TypeError):
            content = ""
            raw_calls = []
        was_empty = bool((completion_tokens or 0) > 0 and not content and not raw_calls)
        tool_calls_made = _normalize_tool_calls(raw_calls, classification.sensitivity)

    response_captured = capture_response(upstream_resp, classification.sensitivity)
    spool = getattr(request.app.state, "outcome_spool", None)
    pricing = getattr(request.app.state, "pricing", None)
    response_size_bytes = (
        len(json.dumps(upstream_resp, ensure_ascii=False).encode("utf-8"))
        if isinstance(upstream_resp, dict)
        else None
    )
    actual_model = upstream_resp.get("model") if isinstance(upstream_resp, dict) else None
    actual_provider = upstream_resp.get("provider") if isinstance(upstream_resp, dict) else None
    cost_provider = _resolve_pricing_provider(decision_provider, actual_provider, decision_model)
    cost_usd = (
        pricing.compute_cost(
            cost_provider,
            decision_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        if pricing is not None
        else None
    )
    await persist_outcome(
        pool,
        spool,
        decision_id=decision_id,
        status_code=upstream_status,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        prefix_hash=cache_prefix_hash(payload),
        cost_usd=cost_usd,
        was_empty=was_empty,
        response_body=response_captured.body,
        response_body_truncated_at_byte=response_captured.truncated_at_byte,
        response_size_bytes=response_size_bytes,
        tool_calls_made=tool_calls_made,
        actual_model=actual_model,
        actual_provider=actual_provider,
    )
    # Brain layer — fire-and-forget. Compute bloat findings + update scorecard.
    # Wrapped in try/except so a brain failure never breaks the request path.
    if pool is not None:
        try:
            from app.drift_engine import process_drift
            from app.scorecard import process_brain
            await process_brain(
                pool, pricing,
                decision_id=decision_id,
                actual_provider=actual_provider,
                actual_model=actual_model,
            )
            await process_drift(pool, decision_id=decision_id)
            # SecondBrain memory ingest (opt-in via NAUTGATE_SB_INGEST=true).
            # Replaces the role FlowAI used to play — flow-proxy can retire
            # once this is enabled in production.
            from app.sb_memory import ingest_outcome as _sb_ingest
            await _sb_ingest(
                app_pool=pool,
                agent_id=agent_id,
                session_id=session_id,
                model=actual_model or decision_model,
                prompt_body=captured.body,
                response_body=response_captured.body,
            )
            # Quality eval — sampled + 100% on anomalies. Cheap judge model,
            # capped daily spend, never blocks the request.
            from app.quality_eval import process_quality as _process_quality
            await _process_quality(
                pool,
                decision_id=decision_id,
                judge_client=getattr(request.app.state, "quality_judge", None),
                pricing=pricing,
            )
        except Exception as exc:
            log.warning("brain_layer_failed", error=str(exc), decision_id=str(decision_id))

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

    # PLUGINS: on_response + on_outcome (fire-and-forget). after_route fires after JSON build.
    if plugins is not None and not plugins.is_empty:
        plugins.dispatch_on_response(
            {
                "decision_id": decision_id,
                "response_body": response_captured.body,
                "was_empty": was_empty,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "status_code": upstream_status,
            }
        )
        plugins.dispatch_on_outcome(
            {
                "decision_id": decision_id,
                "status_code": upstream_status,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "was_empty": was_empty,
                "decision_provider": decision_provider,
                "decision_model": decision_model,
            }
        )

    if upstream_resp is None:
        raise HTTPException(status_code=502, detail="upstream_failed")

    final = (
        response_translator(upstream_resp, decision_model) if response_translator else upstream_resp
    )

    headers = dict(common_headers)
    headers["X-Nautgate-Latency-Ms"] = str(duration_ms)
    headers["X-Nautgate-Was-Empty"] = "true" if was_empty else "false"

    # PLUGINS: after_route — response is now ready to deliver.
    if plugins is not None and not plugins.is_empty:
        plugins.dispatch_after_route(
            {
                "decision_id": decision_id,
                "status_code": upstream_status,
                "duration_ms": duration_ms,
            }
        )

    return JSONResponse(content=final, headers=headers)


def _streaming_response(
    *,
    request: Request,
    payload: dict,
    decision_id: uuid.UUID,
    started: float,
    decision_provider: str,
    decision_model: str,
    classification_sensitivity: str,
    health_tracker,
    pool,
    nautrouter,
    common_headers: dict,
    stream_translator: Callable[[bytes], list[bytes]] | None,
    stream_translator_finish: Callable[[], list[bytes]] | None,
    # New: passed through to the post-outcome SB ingest hook.
    agent_id: str | None = None,
    session_id: str | None = None,
    captured_prompt_body: str | None = None,
) -> StreamingResponse:
    """Tee + cap + (optional) inline format translation. Tech Paper §11."""
    capture = StreamCapture(cap_bytes=ACCUMULATOR_CAP_BYTES_DEFAULT)
    state = {"first_byte_ms": None, "client_disconnected": False}

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in nautrouter.chat_completions_stream(payload):
                if state["first_byte_ms"] is None:
                    state["first_byte_ms"] = int((time.monotonic() - started) * 1000)
                # Capture the upstream (canonical) bytes for parsing/audit.
                capture.append(chunk)
                # If a translator is active, what the client sees is the translated form.
                if stream_translator is None:
                    yield chunk
                else:
                    for out in stream_translator(chunk):
                        yield out
            # Upstream finished cleanly. If a translator buffered partial state, flush.
            if stream_translator_finish is not None:
                for out in stream_translator_finish():
                    yield out
        except asyncio.CancelledError:
            state["client_disconnected"] = True
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            parsed = parse_sse_for_outcome(bytes(capture.accumulator))
            response_captured = capture_response(
                parsed.get("assembled_content"), classification_sensitivity
            )
            spool = getattr(request.app.state, "outcome_spool", None)
            stream_pricing = getattr(request.app.state, "pricing", None)
            stream_cost_provider = _resolve_pricing_provider(
                decision_provider,
                parsed.get("actual_provider"),
                decision_model,
            )
            stream_cost_usd = (
                stream_pricing.compute_cost(
                    stream_cost_provider,
                    decision_model,
                    prompt_tokens=parsed.get("prompt_tokens"),
                    completion_tokens=parsed.get("completion_tokens"),
                    cache_read_tokens=parsed.get("cache_read_tokens"),
                    cache_write_tokens=parsed.get("cache_write_tokens"),
                )
                if stream_pricing is not None
                else None
            )
            try:
                stream_tool_calls = _normalize_tool_calls(
                    parsed.get("tool_calls") or [], classification_sensitivity
                )
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
                    cache_read_tokens=parsed.get("cache_read_tokens"),
                    cache_write_tokens=parsed.get("cache_write_tokens"),
                    prefix_hash=cache_prefix_hash(payload),
                    cost_usd=stream_cost_usd,
                    was_empty=parsed.get("was_empty", False),
                    was_truncated=capture.was_truncated,
                    truncated_at_byte=capture.truncated_at_byte,
                    client_disconnected=state["client_disconnected"],
                    response_body=response_captured.body,
                    response_body_truncated_at_byte=response_captured.truncated_at_byte,
                    response_size_bytes=capture.bytes_seen,
                    tool_calls_made=stream_tool_calls,
                    actual_model=parsed.get("actual_model"),
                    actual_provider=parsed.get("actual_provider"),
                )
                # Brain layer — same fire-and-forget pattern as non-streaming.
                if pool is not None:
                    try:
                        from app.drift_engine import process_drift
                        from app.scorecard import process_brain
                        await process_brain(
                            pool, stream_pricing,
                            decision_id=decision_id,
                            actual_provider=parsed.get("actual_provider"),
                            actual_model=parsed.get("actual_model"),
                        )
                        await process_drift(pool, decision_id=decision_id)
                        # SecondBrain ingest (opt-in via NAUTGATE_SB_INGEST=true)
                        from app.sb_memory import ingest_outcome as _sb_ingest
                        await _sb_ingest(
                            app_pool=pool,
                            agent_id=agent_id or "anonymous",
                            session_id=session_id,
                            model=parsed.get("actual_model") or decision_model,
                            prompt_body=captured_prompt_body,
                            response_body=response_captured.body,
                        )
                        # Quality eval — same hook as non-streaming path.
                        from app.quality_eval import process_quality as _process_quality
                        await _process_quality(
                            pool,
                            decision_id=decision_id,
                            judge_client=getattr(request.app.state, "quality_judge", None),
                            pricing=stream_pricing,
                        )
                    except Exception as exc:
                        log.warning("brain_layer_failed_in_stream", error=str(exc), decision_id=str(decision_id))
            except Exception as exc:
                log.error(
                    "outcome_write_failed_in_stream",
                    error=str(exc),
                    decision_id=str(decision_id),
                )

            if decision_provider != "passthrough":
                if health_tracker is not None:
                    health_tracker.record(
                        decision_provider, decision_model, was_empty=parsed.get("was_empty", False)
                    )
                try:
                    await upsert_health(
                        pool,
                        provider=decision_provider,
                        model=decision_model,
                        duration_ms=duration_ms,
                        was_empty=parsed.get("was_empty", False),
                        success=not state["client_disconnected"],
                    )
                except Exception as exc:
                    log.warning("provider_health_upsert_failed_stream", error=str(exc))

            # PLUGINS: stream finished — fire on_response, after_route, on_outcome.
            stream_plugins = getattr(request.state, "plugins", None)
            if stream_plugins is not None and not stream_plugins.is_empty:
                base_payload = {
                    "decision_id": decision_id,
                    "status_code": 200,
                    "duration_ms": duration_ms,
                    "first_byte_ms": state["first_byte_ms"],
                    "prompt_tokens": parsed.get("prompt_tokens"),
                    "completion_tokens": parsed.get("completion_tokens"),
                    "was_empty": parsed.get("was_empty", False),
                    "was_truncated": capture.was_truncated,
                    "client_disconnected": state["client_disconnected"],
                    "decision_provider": decision_provider,
                    "decision_model": decision_model,
                }
                stream_plugins.dispatch_on_response(
                    {**base_payload, "response_body": response_captured.body}
                )
                stream_plugins.dispatch_after_route(base_payload)
                stream_plugins.dispatch_on_outcome(base_payload)

    headers = dict(common_headers)
    headers.update(
        {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ============================================================================
# OpenAI Chat
# ============================================================================


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    # ChatGPT-OAuth bypass — if Codex (or anyone) sends with chatgpt-account-id,
    # forward transparently to chatgpt.com. See app/oauth_forwarder.py.
    from app.oauth_forwarder import forward_to_chatgpt, is_chatgpt_oauth_request
    if is_chatgpt_oauth_request(request):
        return await forward_to_chatgpt(request)

    # Anthropic-OAuth bypass — Claude Code on Max subscription sends an
    # Authorization: Bearer sk-ant-oat01-... token. Forward verbatim to
    # api.anthropic.com so we use the subscription, not the metered key.
    from app.anthropic_oauth_forwarder import (
        forward_to_anthropic,
        is_anthropic_oauth_request,
    )
    if is_anthropic_oauth_request(request):
        return await forward_to_anthropic(request)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    return await _process_chat_request(request, payload=payload, inbound_format="openai_chat")


# ============================================================================
# Anthropic Messages (Week 1b)
# ============================================================================


@router.post("/messages")
async def messages(request: Request) -> Response:
    # Anthropic-OAuth bypass for Claude Code on Max subscription. Detected
    # by the sk-ant-oat01-* token shape and forwarded verbatim to
    # api.anthropic.com so the subscription covers the call.
    from app.anthropic_oauth_forwarder import (
        forward_to_anthropic,
        is_anthropic_oauth_request,
    )
    if is_anthropic_oauth_request(request):
        return await forward_to_anthropic(request)

    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    payload = ant.request_to_openai_chat(raw)

    stream_translator = None
    stream_translator_finish = None
    if payload.get("stream"):
        # Lazy-build the translator on first chunk (we need decision_model, set during DECIDE).
        translator_holder: dict = {"t": None}

        def _ensure():
            if translator_holder["t"] is None:
                model_name = getattr(request.state, "decision_model", payload.get("model", ""))
                translator_holder["t"] = ant.AnthropicStreamTranslator(model=model_name)
            return translator_holder["t"]

        def _on_chunk(chunk: bytes) -> list[bytes]:
            return _ensure().feed(chunk)

        def _on_finish() -> list[bytes]:
            return _ensure().finish()

        stream_translator = _on_chunk
        stream_translator_finish = _on_finish

    return await _process_chat_request(
        request,
        payload=payload,
        inbound_format="anthropic",
        response_translator=ant.response_to_anthropic,
        stream_translator=stream_translator,
        stream_translator_finish=stream_translator_finish,
    )


# ============================================================================
# Stubs (filled later)
# ============================================================================


@router.post("/responses")
async def responses(request: Request) -> Response:
    # ChatGPT-OAuth Codex bypasses the standard pipeline: forwards to
    # chatgpt.com directly, skipping ng_-token auth and NautRouter.
    # Audit row is still written (decision_provider=chatgpt-oauth).
    from app.oauth_forwarder import forward_to_chatgpt, is_chatgpt_oauth_request
    if is_chatgpt_oauth_request(request):
        return await forward_to_chatgpt(request)
    # Anthropic-OAuth handles claude-* requests landing on /v1/responses too.
    from app.anthropic_oauth_forwarder import (
        forward_to_anthropic,
        is_anthropic_oauth_request,
    )
    if is_anthropic_oauth_request(request):
        return await forward_to_anthropic(request)

    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    payload = resp_fmt.request_to_openai_chat(raw)

    stream_translator = None
    stream_translator_finish = None
    if payload.get("stream"):
        translator_holder: dict = {"t": None}

        def _ensure():
            if translator_holder["t"] is None:
                model_name = getattr(request.state, "decision_model", payload.get("model", ""))
                translator_holder["t"] = resp_fmt.ResponsesStreamTranslator(model=model_name)
            return translator_holder["t"]

        def _on_chunk(chunk: bytes) -> list[bytes]:
            return _ensure().feed(chunk)

        def _on_finish() -> list[bytes]:
            # Build the translator even on empty upstream so terminator events fire.
            return _ensure().finish()

        stream_translator = _on_chunk
        stream_translator_finish = _on_finish

    return await _process_chat_request(
        request,
        payload=payload,
        inbound_format="openai_responses",
        response_translator=resp_fmt.response_to_openai_responses,
        stream_translator=stream_translator,
        stream_translator_finish=stream_translator_finish,
    )


@router.get("/models")
async def list_models(request: Request) -> Response:
    """Lists models available via NautGate.

    Composed from the routing table (each tier's primary + fallback) plus the
    synthetic ``auto`` entry. Annotates `nautgate_unhealthy: true` for any
    (provider, model) the in-process health tracker has demoted.

    Shape mirrors OpenAI's `/v1/models`: `{object, data: [{id, object, owned_by, ...}]}`.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    table = getattr(request.app.state, "routing_table", None) or {}
    tracker = getattr(request.app.state, "health_tracker", None)

    seen: dict[tuple[str, str], dict] = {}
    tier_seen: dict[tuple[str, str], list[str]] = {}
    for tier_name, body in table.items():
        for slot in ("primary", "fallback"):
            entry = body.get(slot) if isinstance(body, dict) else None
            if not isinstance(entry, dict):
                continue
            provider = entry.get("provider")
            model = entry.get("model")
            if not provider or not model:
                continue
            key = (provider, model)
            tier_seen.setdefault(key, []).append(f"{tier_name}:{slot}")
            if key not in seen:
                seen[key] = {
                    "id": model,
                    "object": "model",
                    "owned_by": provider,
                    "nautgate_provider": provider,
                    "nautgate_tiers": [],
                }

    for key, item in seen.items():
        item["nautgate_tiers"] = tier_seen.get(key, [])
        if tracker is not None and tracker.is_unhealthy(*key):
            item["nautgate_unhealthy"] = True

    data = sorted(seen.values(), key=lambda m: (m["nautgate_provider"], m["id"]))
    # Synthetic "auto" entry — NautGate's tier-driven router.
    data.insert(
        0,
        {
            "id": "auto",
            "object": "model",
            "owned_by": "nautgate",
            "nautgate_provider": "nautgate",
            "nautgate_tiers": list(table.keys()),
        },
    )
    return JSONResponse({"object": "list", "data": data})


@router.get("/findings/summary")
async def findings_summary(request: Request) -> Response:
    """Lighthouse-style privacy audit aggregation for the authenticated agent.

    Query params:
      hours       : 1..720 (default 168 = 7 days)
      scan_limit  : 1..2000 (default 500) — max recent decisions to scan

    Returns: {overall, verdict, verdict_explain, cat_scores, cat_counts,
              host_matrix, type_matrix, scanned_count}
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)

    try:
        hours = int(request.query_params.get("hours", "168"))
        scan_limit = int(request.query_params.get("scan_limit", "500"))
    except ValueError:
        raise HTTPException(
            status_code=400, detail="hours and scan_limit must be integers"
        ) from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")
    if scan_limit < 1 or scan_limit > 2000:
        raise HTTPException(status_code=400, detail="scan_limit must be in 1..2000")

    decisions = await queries.get_decisions_for_findings_scan(
        pool, agent_id=agent_id, hours=hours, limit=scan_limit
    )
    return JSONResponse(build_audit(decisions))


def _resolve_cost_scope(request: Request, authed_agent: str) -> str | None:
    """Resolve the ``agent_id`` cost-query param.

    - omitted          → authenticated agent (current behavior)
    - ``"*"`` or "all" → aggregate across all agents
    - any other        → that specific agent (any authenticated holder may query
                         any agent's cost; the dashboard is admin-grade).
    """
    raw = request.query_params.get("agent_id")
    if not raw:
        return authed_agent
    if raw in ("*", "all", "ALL"):
        return "*"
    return raw


def _resolve_project_scope(request: Request) -> str | None:
    """``?project_id=foo`` filters; ``*`` or absent → no project filter."""
    raw = request.query_params.get("project_id")
    if not raw or raw in ("*", "all", "ALL"):
        return None
    return raw


@router.get("/agents")
async def list_agents(request: Request) -> Response:
    """Distinct agent_ids from api_keys + 30-day call counts. Drives the
    Cost-tab dropdown.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    items = await queries.get_agents_with_key_counts(pool)
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/projects")
async def list_projects(request: Request) -> Response:
    """Distinct projects (cost centers) with their keys, agents, 30-day
    activity, and cumulative spend. Drives the project dropdown / panel.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    items = await queries.get_projects_with_stats(pool)
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/cost/summary")
async def cost_summary(request: Request) -> Response:
    """Total cost + by-provider/model/tier breakdowns over the last `?hours=N`.

    Optional ``?agent_id=*`` aggregates across all agents.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    authed_agent = await authenticate(pool, request)
    scope = _resolve_cost_scope(request, authed_agent)

    try:
        hours = int(request.query_params.get("hours", "24"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    project_scope = _resolve_project_scope(request)
    return JSONResponse(await queries.get_cost_summary(
        pool, agent_id=scope, hours=hours, project_id=project_scope,
    ))


@router.get("/cost/timeseries")
async def cost_timeseries(request: Request) -> Response:
    """Bucketed cost time-series for the line chart.

    Query params:
      bucket   : "hour" (default) or "day"
      hours    : 1..720 (default 168 = 7 days)
      agent_id : optional; ``*`` for all, otherwise specific agent
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    authed_agent = await authenticate(pool, request)
    scope = _resolve_cost_scope(request, authed_agent)

    bucket = request.query_params.get("bucket", "hour")
    if bucket not in ("hour", "day"):
        raise HTTPException(status_code=400, detail="bucket must be 'hour' or 'day'")

    try:
        hours = int(request.query_params.get("hours", "168"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    project_scope = _resolve_project_scope(request)
    return JSONResponse(
        await queries.get_cost_timeseries(
            pool, agent_id=scope, bucket=bucket, hours=hours, project_id=project_scope,
        )
    )


def _price_for_model(pricing, model: str | None):
    """Resolve a ModelPrice for a bare model name by trying known providers.

    The cache summary groups by decision_model only; pricing keys are
    "<provider>/<model>". Try the provider families until one resolves.
    """
    if pricing is None or not model:
        return None
    for prov in ("anthropic", "openai", "openrouter", "deepseek", "gemini"):
        p = pricing.lookup(prov, model)
        if p is not None:
            return p
    return None


def _cache_costs(pricing, model: str | None, fresh_tk: int, read_tk: int,
                 write_tk: int) -> dict | None:
    """Cache-off vs cache-on input cost for this model's volume.

    off   = (fresh + read + write) × input_rate    [what it'd cost with no cache]
    on    = fresh×input + read×cache_read_rate + write×cache_write_rate
    saved = off − on  (positive = caching is a net win)

    Input-side only — completion cost is identical either way, so it cancels.
    """
    price = _price_for_model(pricing, model)
    if price is None:
        return None
    read_rate = price.cache_read if price.cache_read is not None else price.input
    write_rate = price.cache_write if price.cache_write is not None else price.input
    off = (fresh_tk + read_tk + write_tk) * price.input / 1_000_000
    on = (
        fresh_tk * price.input + read_tk * read_rate + write_tk * write_rate
    ) / 1_000_000
    return {
        "cache_off_usd": round(off, 6),
        "cache_on_usd": round(on, 6),
        "saved_usd": round(off - on, 6),
    }


@router.get("/cache/summary")
async def cache_summary(request: Request) -> Response:
    """Prompt-cache accounting: hit-rate, token split, and real $ saved.

    Gateway-wide (single-operator). Optional ``?model=<name>`` filter and
    ``?hours=N`` (default 24, max 720).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    try:
        hours = int(request.query_params.get("hours", "24"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")
    model_filter = request.query_params.get("model")

    summary = await queries.get_cache_summary(pool, hours=hours, model_filter=model_filter)
    pricing = getattr(request.app.state, "pricing", None)

    # Attach cache-off / cache-on / saved per model + totals, using live pricing.
    tot_off = tot_on = 0.0
    have_any = False
    for row in summary["by_model"]:
        costs = _cache_costs(
            pricing, row["model"], row["fresh_tokens"],
            row["cache_read_tokens"], row["cache_write_tokens"],
        )
        if costs is None:
            row["cache_off_usd"] = row["cache_on_usd"] = row["saved_usd"] = None
            continue
        row.update(costs)
        tot_off += costs["cache_off_usd"]
        tot_on += costs["cache_on_usd"]
        have_any = True
    t = summary["totals"]
    t["cache_off_usd"] = round(tot_off, 6) if have_any else None
    t["cache_on_usd"] = round(tot_on, 6) if have_any else None
    t["saved_usd"] = round(tot_off - tot_on, 6) if have_any else None
    return JSONResponse(summary)


@router.get("/cache/prefixes")
async def cache_prefixes(request: Request) -> Response:
    """Cacheable-prefix reuse: top-reused prefixes + 'leaky' write-heavy ones.

    The leak list surfaces prompts that write to cache but rarely read back —
    usually a timestamp/ID mutating an otherwise-stable prefix.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    try:
        hours = int(request.query_params.get("hours", "168"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    return JSONResponse(await queries.get_prefix_reuse(pool, hours=hours))


@router.get("/decisions/recent")
async def decisions_recent(request: Request) -> Response:
    """Recent route_decisions joined with outcomes.

    By default scopes to the authenticated agent. An optional
    ``?agent_id=<x>`` query param overrides the scope so the dashboard
    can view auto-discovered OAuth sessions (claude-oauth-…, codex-…)
    that have no ng_ token of their own to authenticate with. Any valid
    ng_ token may request any agent_id — single-tenant assumption.

    Default ?limit=50, max 200.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    caller_agent = await authenticate(pool, request)
    target_agent = request.query_params.get("agent_id", "").strip() or caller_agent

    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        raise HTTPException(status_code=400, detail="limit must be an integer") from None
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be in 1..200")

    rows = await queries.get_recent_decisions(pool, agent_id=target_agent, limit=limit)
    return JSONResponse({"agent_id": target_agent, "limit": limit, "data": rows})


@router.get("/agents/discovered")
async def agents_discovered(request: Request) -> Response:
    """List of agent_ids that have produced traffic recently.

    The dashboard calls this on startup and merges any unseen agent_id
    into its localStorage session list as a discovered entry (no token,
    scope-only). That's how OAuth-derived sessions (Claude Max,
    ChatGPT Max) show up in the picker without manual setup.

    Default ?hours=168 (7 days), max 720 (30 days).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        hours = int(request.query_params.get("hours", "168"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    rows = await queries.get_discovered_agents(pool, hours=hours)
    return JSONResponse({"hours": hours, "data": rows})


@router.get("/decisions/{decision_id}")
async def decision_detail(decision_id: str, request: Request) -> Response:
    """Full metadata for a single decision: classification, score, signals,
    brain hints, prompt + response bodies (subject to capture policy),
    outcome metrics, cost.

    Same scope rules as /decisions/recent: defaults to the authenticated
    caller's agent_id; an optional ?agent_id=<x> overrides so the
    dashboard can open detail drawers for rows in discovered OAuth
    sessions that don't own an ng_ token.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    caller_agent = await authenticate(pool, request)
    target_agent = request.query_params.get("agent_id", "").strip() or caller_agent

    try:
        uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad decision_id") from None

    row = await queries.get_decision_detail(pool, agent_id=target_agent, decision_id=decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return JSONResponse(row)


@router.get("/scorecard")
async def scorecard_view(request: Request) -> Response:
    """Brain layer scorecard — per-(provider, model, tier) score, sample count,
    cumulative wasted USD, and recent incidents linking back to the offending
    decisions.

    Auth required, but scorecard data is global (not per-agent) — every
    authenticated agent sees the same brain memory.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.scorecard import get_scorecard_with_incidents
    rows = await get_scorecard_with_incidents(pool, incidents_per_row=5)
    return JSONResponse({"items": rows})


@router.get("/scorecard/{provider}/{model_path:path}/incidents")
async def scorecard_incidents(provider: str, model_path: str, request: Request) -> Response:
    """Paginated incident list for one (provider, model). The model_path can
    contain slashes (openrouter/deepseek/deepseek-v4-flash), hence path:.
    Query params: tier (required), limit (default 50, max 500).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    tier = request.query_params.get("tier")
    if not tier:
        raise HTTPException(status_code=400, detail="tier query param required")
    try:
        limit = min(500, max(1, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = await pool.fetch(
        """
        SELECT id::text, decision_id::text, finding_type, severity,
               score_penalty, estimated_waste_usd, ts
          FROM nautgate.model_incidents
         WHERE provider = $1 AND model = $2 AND tier = $3
         ORDER BY ts DESC
         LIMIT $4
        """,
        provider, model_path, tier, limit,
    )
    items = [
        {
            "id": r["id"],
            "decision_id": r["decision_id"],
            "finding_type": r["finding_type"],
            "severity": r["severity"],
            "score_penalty": float(r["score_penalty"]),
            "estimated_waste_usd": float(r["estimated_waste_usd"] or 0),
            "ts": r["ts"].isoformat() if r["ts"] else None,
        }
        for r in rows
    ]
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/backups")
async def list_backups_endpoint(request: Request) -> Response:
    """List recent backups (newest first)."""
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.backup import list_backups
    try:
        limit = min(500, max(1, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    items = await list_backups(pool, limit=limit)
    return JSONResponse({"items": items, "count": len(items)})


@router.post("/backups")
async def create_backup_endpoint(request: Request) -> Response:
    """Trigger an immediate backup (created_via='manual')."""
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.backup import create_backup
    try:
        row = await create_backup(pool, via="manual")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"backup_failed: {exc}") from None
    return JSONResponse(row, status_code=201)


# /backups/config endpoints MUST come before the parametric /backups/{id}
# routes so they're matched as literal paths (otherwise "config" would be
# parsed as a backup_id UUID).
@router.get("/backups/config")
async def get_backup_config_endpoint(request: Request) -> Response:
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.backup import get_config
    return JSONResponse(await get_config(pool))


@router.put("/backups/config")
async def put_backup_config_endpoint(request: Request) -> Response:
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    from app.backup import update_config
    try:
        cfg = await update_config(
            pool,
            enabled=body.get("enabled"),
            interval_hours=body.get("interval_hours"),
            retention_count=body.get("retention_count"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(cfg)


@router.delete("/backups/{backup_id}")
async def delete_backup_endpoint(backup_id: str, request: Request) -> Response:
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        bid = uuid.UUID(backup_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad backup_id") from None
    from app.backup import delete_backup
    ok = await delete_backup(pool, bid)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return JSONResponse({"deleted": True})


@router.post("/backups/{backup_id}/restore")
async def restore_backup_endpoint(backup_id: str, request: Request) -> Response:
    """DESTRUCTIVE: drops the nautgate schema and reloads from the backup.

    Requires JSON body {"confirm": true} so a stray click can't wipe data.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="confirm=true required in body")
    try:
        bid = uuid.UUID(backup_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad backup_id") from None
    from app.backup import restore_backup
    try:
        await restore_backup(pool, bid)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"restore_failed: {exc}") from None
    return JSONResponse({"restored": True, "backup_id": backup_id})


@router.get("/config")
async def get_config_endpoint(request: Request) -> Response:
    """Return runtime-tunable app settings (currently: SB ingest config)."""
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.app_config import get_settings
    s = await get_settings(pool)
    # Never expose secrets via GET — strip any password field we'd added by accident.
    if isinstance(s.get("sb_ingest"), dict):
        s["sb_ingest"].pop("password", None)
    return JSONResponse(s)


@router.put("/config")
async def put_config_endpoint(request: Request) -> Response:
    """Patch runtime-tunable settings. Body: partial JSON like
    ``{"sb_ingest": {"enabled": true, "host": "100.71.163.122"}}``.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        patch = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # Never accept passwords through this endpoint.
    if isinstance(patch.get("sb_ingest"), dict):
        patch["sb_ingest"].pop("password", None)
    # Never accept API keys through this endpoint either — judge_api_key_env
    # lets ops point at a different env var name; the secret itself stays in env.
    if isinstance(patch.get("quality_eval"), dict):
        patch["quality_eval"].pop("api_key", None)
    from app.app_config import update_settings
    from app.quality_eval import config_cache_clear as qe_cache_clear
    from app.sb_memory import config_cache_clear
    merged = await update_settings(pool, patch)
    config_cache_clear()      # next sb_memory ingest re-reads from DB
    qe_cache_clear()          # next quality_eval call re-reads from DB
    if isinstance(merged.get("sb_ingest"), dict):
        merged["sb_ingest"].pop("password", None)
    if isinstance(merged.get("quality_eval"), dict):
        merged["quality_eval"].pop("api_key", None)
    return JSONResponse(merged)


@router.post("/config/sb-ingest/test")
async def test_sb_ingest_endpoint(request: Request) -> Response:
    """Open a quick connection to the configured SB DB and report ok/error.

    Uses the *current* saved config (DB + env merge) — i.e. tests what
    ingest would actually use right now. Run *after* saving changes.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.app_config import sb_ingest_config
    from app.sb_memory import test_connection
    cfg = await sb_ingest_config(pool)
    ok, detail = await test_connection(cfg)
    return JSONResponse({"ok": ok, "detail": detail, "host": cfg.get("host"), "port": cfg.get("port"), "database": cfg.get("database")}, status_code=200 if ok else 502)


@router.get("/quality/models")
async def quality_models(request: Request) -> Response:
    """List models available from the configured judge provider.

    Proxies to `<judge_base_url>/v1/models` using the configured API key.
    Returns a flat list of ``{id, name, context_length, prompt_price_per_m,
    completion_price_per_m}`` sorted by ascending input price so the cheapest
    viable judges appear first.

    Optional query params:
      - ``provider``: try a different provider than the saved one (e.g.
        previewing models before saving).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    from app.app_config import QUALITY_PROVIDER_PRESETS, quality_eval_config
    cfg = await quality_eval_config(pool)
    # Allow ?provider= preview without saving — useful for the UI dropdown.
    preview_provider = request.query_params.get("provider")
    if preview_provider and preview_provider != cfg.get("judge_provider"):
        preset = QUALITY_PROVIDER_PRESETS.get(preview_provider.lower(), {})
        cfg = {
            **cfg,
            "judge_provider": preview_provider,
            "judge_base_url": preset.get("base_url") or cfg.get("judge_base_url"),
            "api_key": __import__("os").environ.get(preset.get("api_key_env") or "", ""),
        }

    base_url = (cfg.get("judge_base_url") or "").rstrip("/")
    if not base_url:
        return JSONResponse({"provider": cfg.get("judge_provider"), "models": [],
                             "error": "no_base_url"})
    # Tolerate base_url with or without a trailing /v1 (LMStudio's default
    # bundles /v1 in LMSTUDIO_BASE_URL).
    if base_url.endswith("/v1"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    headers = {}
    api_key = cfg.get("api_key") or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    client = getattr(request.app.state, "quality_judge", None)
    if client is None:
        raise HTTPException(status_code=503, detail="judge_client_unavailable")

    try:
        resp = await client.get(models_url, headers=headers, timeout=8.0)
    except Exception as exc:
        return JSONResponse({"provider": cfg.get("judge_provider"), "models": [],
                             "error": f"fetch_failed: {type(exc).__name__}"},
                            status_code=502)
    if resp.status_code >= 400:
        return JSONResponse({"provider": cfg.get("judge_provider"), "models": [],
                             "error": f"http_{resp.status_code}",
                             "detail": resp.text[:200]},
                            status_code=502)
    try:
        payload = resp.json()
    except Exception:
        return JSONResponse({"provider": cfg.get("judge_provider"), "models": [],
                             "error": "bad_json"}, status_code=502)

    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return JSONResponse({"provider": cfg.get("judge_provider"), "models": [],
                             "error": "unexpected_shape"}, status_code=502)

    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if not isinstance(mid, str):
            continue
        # OpenRouter returns prices as strings; OpenAI doesn't include pricing.
        pricing = m.get("pricing") or {}
        def _pm(v):
            # Convert USD-per-token → USD-per-million for display.
            # OpenRouter returns -1 for variable/auto-priced models — treat
            # those as "unknown" so they don't sort to the very top as
            # though they were profoundly cheap.
            try:
                if v in (None, ""):
                    return None
                f = float(v)
                if f < 0:
                    return None
                return round(f * 1_000_000, 4)
            except (TypeError, ValueError):
                return None
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": m.get("context_length") or m.get("context_window"),
            "prompt_price_per_m": _pm(pricing.get("prompt")),
            "completion_price_per_m": _pm(pricing.get("completion")),
            "supports_response_format": bool(
                m.get("supported_parameters") or m.get("supported_features") or []
            ),
        })
    # Sort: priced models cheapest-first, then unpriced (OpenAI / LMStudio) alphabetically.
    def _sort_key(m):
        p = m.get("prompt_price_per_m")
        return (0 if p is not None else 1, p if p is not None else 0, m["id"])
    out.sort(key=_sort_key)

    return JSONResponse({
        "provider": cfg.get("judge_provider"),
        "base_url": base_url,
        "model_count": len(out),
        "models": out,
    })


@router.get("/quality/health")
async def quality_health(request: Request) -> Response:
    """Judge health snapshot — succeeded vs failed evaluations, avg latency,
    spend today vs daily cap, last error. Surfaced at the top of the Quality
    tab so the operator can see at a glance whether the judge is alive.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    from app.app_config import quality_eval_config
    cfg = await quality_eval_config(pool)

    async with pool.acquire() as conn:
        # 24h activity from the evals table itself.
        row24 = await conn.fetchrow(
            """
            SELECT COUNT(*)                                                 AS attempts,
                   SUM(CASE WHEN rubric IS NOT NULL THEN 1 ELSE 0 END)::int AS succeeded,
                   AVG(judge_latency_ms)::int                              AS avg_latency_ms,
                   MAX(ts)                                                  AS last_eval_at
              FROM nautgate.quality_evals
             WHERE ts > NOW() - INTERVAL '24 hours'
            """,
        )
        # Today's spend (UTC day, matches the cap window).
        spend_today_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(judge_cost_usd), 0)::FLOAT AS s "
            "FROM nautgate.quality_evals WHERE date(ts) = current_date",
        )
        # Distribution by trigger so operator sees where evals are coming from.
        by_trigger = await conn.fetch(
            """
            SELECT trigger, COUNT(*) AS n
              FROM nautgate.quality_evals
             WHERE ts > NOW() - INTERVAL '7 days'
             GROUP BY trigger
             ORDER BY n DESC
            """,
        )
        total_evals_row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM nautgate.quality_evals",
        )

    attempts = int((row24 or {}).get("attempts") or 0)
    succeeded = int((row24 or {}).get("succeeded") or 0)
    success_rate = (succeeded / attempts) if attempts else None
    spend_today = float((spend_today_row or {}).get("s") or 0.0)
    cap = float(cfg.get("daily_cost_cap_usd") or 0.0)

    return JSONResponse({
        "enabled": bool(cfg.get("enabled", True)),
        "judge_provider": cfg.get("judge_provider"),
        "judge_model": cfg.get("judge_model"),
        "judge_base_url": cfg.get("judge_base_url"),
        "api_key_configured": bool(cfg.get("api_key")),
        "sample_rate": float(cfg.get("sample_rate") or 0.0),
        "daily_cost_cap_usd": cap,
        "spend_today_usd": spend_today,
        "spend_today_pct_of_cap": (spend_today / cap * 100.0) if cap > 0 else None,
        "last_24h": {
            "attempts": attempts,
            "succeeded": succeeded,
            "failed": attempts - succeeded,
            "success_rate": success_rate,
            "avg_latency_ms": (row24 or {}).get("avg_latency_ms"),
            "last_eval_at": (row24 or {}).get("last_eval_at").isoformat()
                if (row24 or {}).get("last_eval_at") else None,
        },
        "by_trigger_7d": [{"trigger": r["trigger"], "n": int(r["n"])} for r in by_trigger],
        "total_evaluations_ever": int((total_evals_row or {}).get("n") or 0),
    })


# Rule-based clustering of raw anti_pattern strings into canonical buckets.
# The judge emits free-form one-phrase descriptions; many of them are
# variants of the same underlying mistake ("Misunderstood the task context",
# "Misunderstood the request requirements", "Misunderstood the focus").
# Rather than embedding-clustering live, we use keyword heuristics that the
# operator can edit. Each cluster has a list of trigger keywords; first
# match wins. Unmatched patterns stay visible under "Other (raw)".
#
# Order matters: more-specific clusters MUST come before more-general ones.

_ANTI_PATTERN_CLUSTERS: list[tuple[str, list[str]]] = [
    (
        "Multi-task prompt — model executed part, dropped the rest",
        ["multi_task", "multiple things", "asked for n ", "three things",
         "two things", "first part but", "did one but", "partial execution"],
    ),
    (
        'Vague scope — said "check"/"review" without saying what to inspect',
        ["vague request", "without specifics", "without specifying",
         "without details", "no clear scope", "no specific scope",
         "without explicit"],
    ),
    (
        "No specific task or action requested — open-ended ask",
        ["no specific task", "no specific action", "no clear task",
         "no clear action", "unspecified task", "open-ended"],
    ),
    (
        "Prompt missing explicit requirements / success criteria",
        ["misunderstood the task", "misunderstood the request",
         "misunderstood the specific", "misinterpreted the task",
         "misunderstood the focus", "misunderstood the requirements",
         "misunderstood the update", "misunderstood the verification",
         "missing requirements", "no success criteria"],
    ),
    (
        "Off-topic response — model addressed a different subject",
        ["unrelated information", "different topic", "wrong topic",
         "off-topic", "off topic", "different subject",
         "responded to a different", "addressed unrelated"],
    ),
    (
        "Verification request without acceptance criteria",
        ["verification without", "verify without", "without defining",
         "no acceptance criteria"],
    ),
    (
        "Build / implementation request without spec",
        ["build without", "without specifying details", "without spec",
         "asked for a build", "asked for implementation"],
    ),
    (
        "News / summary request without specifying source",
        ["news without", "summary without source", "without specifying source",
         "current news without"],
    ),
]


def _cluster_anti_pattern(raw: str | None) -> str:
    if not raw:
        return "Other (unspecified)"
    low = raw.lower()
    for canonical, keywords in _ANTI_PATTERN_CLUSTERS:
        for kw in keywords:
            if kw in low:
                return canonical
    return f"Other: {raw[:60]}"


# Tool/function names that indicate one agent delegating work to another.
# When a prompt or response contains a tool_call with one of these names,
# we treat it as evidence of a master → sub-agent edge for the graph.
_DELEGATION_TOOL_NAMES = (
    "coms_send", "comms_send", "Task", "task",
    "dispatch", "delegate", "subagent", "sub_agent",
    "agent_dispatch", "agent_call", "spawn_agent",
    "Agent", "query_experts", "ask_user_question",
)


_TARGET_KEYS_RE = re.compile(
    r'"(target|agent|expert|subagent_type|to|agent_id|recipient)"'
    r'\s*:\s*"([^"\\]{1,60})"'
)


def _extract_targets_regex_fallback(args_str: str) -> list[str]:
    """When the tool-call ``arguments`` string is truncated mid-JSON (we cap
    at 200 bytes for storage), the structured parser fails. Fall back to a
    regex that scans for the first key/value pair we care about — works even
    when the rest of the JSON is missing.
    """
    out: list[str] = []
    for m in _TARGET_KEYS_RE.finditer(args_str):
        val = m.group(2).strip()
        if val:
            out.append(val[:60])
    return out


def _extract_targets(tool_calls: list) -> list[str]:
    """Return every delegation target found in this list of tool calls.

    Handles three storage cases:
      1. ``arguments`` is a fully-valid JSON string  → parse it
      2. ``arguments`` is truncated JSON             → regex-scan the prefix
      3. ``arguments`` is already a dict             → use directly

    Different agent stacks use different shapes:
      - Pi:        ``subagent {"agent": "discovery-scout"}`` or chain form
                   ``subagent {"chain": [{"agent": "scout"}, …]}``
      - Pi:        ``coms_send {"target": "documenter"}``
      - Pi:        ``query_experts {"queries": [{"expert": "config-expert"}, …]}``
      - Claude:    ``Agent {"subagent_type": "code-reviewer"}`` or
                   ``Agent {"description": "Plan SecretManager"}``
    """
    if not tool_calls:
        return []
    import json as _json
    out: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        name = (tc.get("name") or "").strip()
        if name not in _DELEGATION_TOOL_NAMES:
            continue
        raw_args = tc.get("arguments")
        args: dict | None = None
        if isinstance(raw_args, str):
            try:
                args = _json.loads(raw_args)
            except (ValueError, TypeError):
                # Truncated JSON — fall back to regex.
                out.extend(_extract_targets_regex_fallback(raw_args))
                args = None
        elif isinstance(raw_args, dict):
            args = raw_args

        if args is not None:
            # Direct single-target keys.
            for key in ("target", "subagent_type", "agent", "to",
                        "agent_id", "recipient"):
                v = args.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip()[:60])

            # Chain form: subagent {"chain": [{"agent": "X"}, …]}.
            chain = args.get("chain")
            if isinstance(chain, list):
                for step in chain:
                    if isinstance(step, dict):
                        a = step.get("agent")
                        if isinstance(a, str) and a.strip():
                            out.append(a.strip()[:60])

            # Multi-expert form: query_experts {"queries": [{"expert": "X"}, …]}.
            queries = args.get("queries")
            if isinstance(queries, list):
                for q in queries:
                    if isinstance(q, dict):
                        e = q.get("expert") or q.get("agent")
                        if isinstance(e, str) and e.strip():
                            out.append(e.strip()[:60])

            # Claude's Task tool with a description but no subagent_type — use
            # a short identifier from the description so we can still graph it.
            if name in ("Agent", "Task") and not args.get("subagent_type"):
                desc = args.get("description")
                if isinstance(desc, str) and desc.strip():
                    short = "-".join(desc.lower().split()[:3])[:40]
                    if short:
                        out.append(f"task:{short}")

    return out


def _extract_target_agent(tool_calls: list) -> str | None:
    """Single-target compat wrapper for older callers."""
    targets = _extract_targets(tool_calls)
    return targets[0] if targets else None


@router.get("/quality/anti-patterns-by-agent")
async def quality_anti_patterns_by_agent(request: Request) -> Response:
    """Per-agent anti-pattern leaderboard. For each agent_id, show its top
    clustered patterns. Answers "which master agent is sending the most
    vague-scope delegations?"
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT q.anti_pattern, q.ts,
                   d.agent_id,
                   (q.rubric->>'task_completion')::numeric AS completion
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE q.anti_pattern IS NOT NULL
               AND q.anti_pattern <> ''
               AND q.ts > NOW() - make_interval(days => $1)
            """,
            days,
        )

    # agent_id → {canonical_cluster → count, completions [...]}
    per_agent: dict[str, dict] = {}
    for r in rows:
        agent = r["agent_id"] or "(unknown)"
        cluster = _cluster_anti_pattern(r["anti_pattern"])
        slot = per_agent.setdefault(agent, {
            "patterns": {}, "completions": [], "total": 0,
        })
        slot["total"] += 1
        slot["patterns"][cluster] = slot["patterns"].get(cluster, 0) + 1
        if r["completion"] is not None:
            slot["completions"].append(float(r["completion"]))

    items: list[dict] = []
    for agent, slot in per_agent.items():
        top = sorted(slot["patterns"].items(), key=lambda x: -x[1])[:5]
        avg = (sum(slot["completions"]) / len(slot["completions"])
               if slot["completions"] else None)
        items.append({
            "agent_id": agent,
            "total_anti_patterns": slot["total"],
            "avg_completion": avg,
            "top_patterns": [{"pattern": p, "count": c} for (p, c) in top],
        })
    items.sort(key=lambda x: -x["total_anti_patterns"])

    return JSONResponse({"window_days": days, "items": items[:30]})


@router.get("/quality/anti-patterns-by-session")
async def quality_anti_patterns_by_session(request: Request) -> Response:
    """Per-session anti-pattern view. Surfaces loop pathologies (one
    session sending 50 bad prompts) and individual bad runs.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        days = int(request.query_params.get("days", "30"))
        min_calls = int(request.query_params.get("min_calls", "5"))
    except ValueError:
        days, min_calls = 30, 5
    days = max(1, min(days, 365))
    min_calls = max(1, min(min_calls, 100))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT q.anti_pattern, q.ts,
                   d.session_id, d.agent_id, d.decision_model,
                   (q.rubric->>'task_completion')::numeric AS completion
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE q.anti_pattern IS NOT NULL
               AND q.anti_pattern <> ''
               AND d.session_id IS NOT NULL
               AND q.ts > NOW() - make_interval(days => $1)
            """,
            days,
        )

    per_session: dict[str, dict] = {}
    for r in rows:
        sid = r["session_id"]
        slot = per_session.setdefault(sid, {
            "patterns": {}, "completions": [], "models": set(),
            "agent_id": r["agent_id"], "first_seen": r["ts"], "last_seen": r["ts"],
        })
        cluster = _cluster_anti_pattern(r["anti_pattern"])
        slot["patterns"][cluster] = slot["patterns"].get(cluster, 0) + 1
        if r["completion"] is not None:
            slot["completions"].append(float(r["completion"]))
        if r["decision_model"]:
            slot["models"].add(r["decision_model"])
        if r["ts"] and (slot["first_seen"] is None or r["ts"] < slot["first_seen"]):
            slot["first_seen"] = r["ts"]
        if r["ts"] and (slot["last_seen"] is None or r["ts"] > slot["last_seen"]):
            slot["last_seen"] = r["ts"]

    items: list[dict] = []
    for sid, slot in per_session.items():
        total = sum(slot["patterns"].values())
        if total < min_calls:
            continue
        top = sorted(slot["patterns"].items(), key=lambda x: -x[1])[:3]
        avg = (sum(slot["completions"]) / len(slot["completions"])
               if slot["completions"] else None)
        items.append({
            "session_id": sid,
            "agent_id": slot["agent_id"],
            "anti_pattern_count": total,
            "avg_completion": avg,
            "first_seen": slot["first_seen"].isoformat() if slot["first_seen"] else None,
            "last_seen": slot["last_seen"].isoformat() if slot["last_seen"] else None,
            "models": list(slot["models"])[:3],
            "top_patterns": [{"pattern": p, "count": c} for (p, c) in top],
        })
    items.sort(key=lambda x: -x["anti_pattern_count"])

    return JSONResponse({
        "window_days": days,
        "min_calls_threshold": min_calls,
        "items": items[:30],
    })


@router.get("/quality/delegation-edges")
async def quality_delegation_edges(request: Request) -> Response:
    """Master → sub-agent delegation graph. Extracts target agent from
    tool_calls_made entries where the tool name matches a known delegation
    pattern (coms_send, Task, dispatch, etc.). Each edge carries call
    count, average task_completion of the SUB-AGENT side, and recent
    failure rate — so the operator can see "Pi → reviewer is producing
    80% partial answers".
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id AS decision_id,
                   d.agent_id,
                   o.tool_calls_made,
                   (q.rubric->>'task_completion')::numeric AS completion,
                   q.anti_pattern
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
              LEFT JOIN nautgate.quality_evals q ON q.decision_id = d.id
             WHERE d.ts > NOW() - make_interval(days => $1)
               AND o.tool_calls_made IS NOT NULL
            """,
            days,
        )

    import json as _json

    # edge → {count, completion_sum, completion_n, failed (low_score) count}
    edges: dict[tuple[str, str], dict] = {}
    for r in rows:
        source = r["agent_id"] or "(unknown)"
        raw_tcs = r["tool_calls_made"]
        if isinstance(raw_tcs, str):
            try:
                tcs = _json.loads(raw_tcs)
            except (ValueError, TypeError):
                tcs = []
        elif isinstance(raw_tcs, list):
            tcs = raw_tcs
        else:
            tcs = []
        targets = _extract_targets(tcs)
        if not targets:
            continue
        for target in targets:
            key = (source, target)
            slot = edges.setdefault(key, {
                "count": 0, "completion_sum": 0.0, "completion_n": 0,
                "low_score_count": 0, "anti_patterns": {},
            })
            slot["count"] += 1
            if r["completion"] is not None:
                slot["completion_sum"] += float(r["completion"])
                slot["completion_n"] += 1
                if float(r["completion"]) < 3.0:
                    slot["low_score_count"] += 1
            if r["anti_pattern"]:
                canonical = _cluster_anti_pattern(r["anti_pattern"])
                slot["anti_patterns"][canonical] = slot["anti_patterns"].get(canonical, 0) + 1

    items: list[dict] = []
    nodes_seen: set[str] = set()
    for (source, target), slot in edges.items():
        nodes_seen.add(source)
        nodes_seen.add(target)
        avg = (slot["completion_sum"] / slot["completion_n"]
               if slot["completion_n"] > 0 else None)
        failure_rate = (slot["low_score_count"] / slot["completion_n"]
                        if slot["completion_n"] > 0 else None)
        top_pat = sorted(slot["anti_patterns"].items(), key=lambda x: -x[1])[:2]
        items.append({
            "source": source,
            "target": target,
            "calls": slot["count"],
            "avg_completion": avg,
            "failure_rate": failure_rate,
            "top_anti_patterns": [
                {"pattern": p, "count": c} for (p, c) in top_pat
            ],
        })
    items.sort(key=lambda x: -x["calls"])

    return JSONResponse({
        "window_days": days,
        "nodes": [{"id": n} for n in sorted(nodes_seen)],
        "edges": items,
    })


@router.get("/quality/anti-patterns")
async def quality_anti_patterns(request: Request) -> Response:
    """Aggregate by clustered ``anti_pattern`` — "what NOT to say to your LLM".

    The judge emits free-form one-phrase descriptions; we cluster them via
    rule-based heuristics (``_ANTI_PATTERN_CLUSTERS``) so variants like
    "Misunderstood the task context" and "Misunderstood the requirements"
    fold into a single canonical bucket the operator can act on.

    Each bucket carries: total occurrences, avg task-completion score, an
    example prompt, the judge's suggested rewrite, and the 3 most common
    raw variants the judge actually emitted (so the operator can spot-check
    the clustering).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    async with pool.acquire() as conn:
        # Pull every eval with an anti_pattern in the window. We'll cluster
        # in Python so the cluster definitions stay in source, not SQL.
        rows = await conn.fetch(
            """
            SELECT q.anti_pattern,
                   q.decision_id,
                   q.ts,
                   q.suggested_prompt,
                   q.coach_notes,
                   (q.rubric->>'task_completion')::numeric AS completion,
                   (q.rubric->>'prompt_clarity')::numeric  AS clarity,
                   d.prompt_excerpt,
                   d.decision_model
              FROM nautgate.quality_evals q
              JOIN nautgate.route_decisions d ON d.id = q.decision_id
             WHERE q.anti_pattern IS NOT NULL
               AND q.anti_pattern <> ''
               AND q.ts > NOW() - make_interval(days => $1)
            """,
            days,
        )

    # Bucket rows by canonical cluster.
    buckets: dict[str, list[dict]] = {}
    raw_variant_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        canonical = _cluster_anti_pattern(r["anti_pattern"])
        buckets.setdefault(canonical, []).append(r)
        raw_variant_counts.setdefault(canonical, {})
        raw_variant_counts[canonical][r["anti_pattern"]] = (
            raw_variant_counts[canonical].get(r["anti_pattern"], 0) + 1
        )

    items: list[dict] = []
    for canonical, rs in buckets.items():
        # Sort within bucket by worst completion first → 3 examples.
        rs_sorted = sorted(
            rs,
            key=lambda x: (
                float(x["completion"]) if x["completion"] is not None else -1.0,
                -x["ts"].timestamp() if x["ts"] else 0,
            ),
        )
        examples = rs_sorted[:3]
        completions = [float(x["completion"]) for x in rs if x["completion"] is not None]
        clarities  = [float(x["clarity"]) for x in rs if x["clarity"] is not None]
        # Top 5 raw variants, sorted by frequency.
        raw_variants = sorted(
            raw_variant_counts[canonical].items(), key=lambda x: -x[1],
        )[:5]
        items.append({
            "anti_pattern":    canonical,
            "occurrences":     len(rs),
            "avg_completion":  sum(completions) / len(completions) if completions else None,
            "avg_clarity":     sum(clarities) / len(clarities) if clarities else None,
            "distinct_models": len({x["decision_model"] for x in rs if x["decision_model"]}),
            "sample_prompts":  [x["prompt_excerpt"] for x in examples if x["prompt_excerpt"]],
            "sample_rewrites": [x["suggested_prompt"] for x in examples
                                if x["suggested_prompt"]],
            "sample_models":   list({x["decision_model"] for x in examples
                                     if x["decision_model"]})[:3],
            "sample_coach":    [x["coach_notes"] for x in examples if x["coach_notes"]],
            "raw_variants":    [{"phrase": p, "count": c} for (p, c) in raw_variants],
        })

    # Sort by occurrences DESC, then worst completion first.
    items.sort(
        key=lambda x: (-x["occurrences"],
                       x["avg_completion"] if x["avg_completion"] is not None else 99),
    )
    items = items[:25]

    return JSONResponse({
        "window_days": days,
        "total_patterns": len(items),
        "items": items,
    })


@router.get("/quality/summary")
async def quality_summary(request: Request) -> Response:
    """Aggregates from nautgate.quality_evals for the Quality tab.

    Query params: ``hours`` (default 24), ``model`` (optional filter; "*" =
    all).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        hours = int(request.query_params.get("hours", "24"))
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 24 * 30))
    model = request.query_params.get("model")
    summary = await queries.get_quality_summary(pool, hours=hours, model_filter=model)
    return JSONResponse(summary)


@router.get("/quality/evaluation/{decision_id}")
async def quality_evaluation_get(decision_id: str, request: Request) -> Response:
    """Return a single eval row (or 404) for the Audit drawer's Coach panel."""
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad decision_id") from None
    row = await queries.get_quality_eval(pool, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no_evaluation")
    # Stringify timestamp for JSON serialization.
    if row.get("ts") is not None:
        row["ts"] = row["ts"].isoformat()
    return JSONResponse(row)


@router.post("/quality/evaluate/{decision_id}")
async def quality_evaluation_run(decision_id: str, request: Request) -> Response:
    """Run the judge on a specific decision *right now*, bypassing sampling.

    Body (optional): ``{"trigger": "manual" | "thumbs_down"}``. Defaults to
    "manual". Honours the daily cost cap and sensitivity gate.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad decision_id") from None
    trigger = "manual"
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("trigger") in ("manual", "thumbs_down"):
            trigger = body["trigger"]
    except Exception:
        pass
    from app.quality_eval import manual_evaluate
    row = await manual_evaluate(
        pool,
        decision_id=uuid.UUID(decision_id),
        judge_client=getattr(request.app.state, "quality_judge", None),
        pricing=getattr(request.app.state, "pricing", None),
        trigger=trigger,
    )
    if row is None:
        raise HTTPException(status_code=422, detail="eval_skipped_or_failed")
    if row.get("ts") is not None:
        row["ts"] = row["ts"].isoformat()
    return JSONResponse(row)


# ── Behavioral analytics — "did the model do what the user asked?" ──────────


@router.get("/behavior/per-model")
async def behavior_per_model(request: Request) -> Response:
    """Per-model behavioral metrics derived from quality_evals.

    Surfaces the cowboy comparison: which model skips Read, edits without
    reading, jumps to action before investigation, retries the same tool.
    Defaults to last 168h (7 days), max 720h (30 days).

    Includes only models with quality evals (so a model still accumulating
    will appear as soon as it has 1 eval; meaningful comparisons need 10+).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        hours = int(request.query_params.get("hours", "168"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")
    rows = await queries.get_behavior_per_model(pool, hours=hours)
    return JSONResponse({"hours": hours, "data": rows})


@router.post("/behavior/compare")
async def behavior_compare_run(request: Request) -> Response:
    """Run the behavioral canary suite through 2+ models via OpenRouter.

    Body (optional):
      {"models": ["anthropic/claude-opus-4-7", "anthropic/claude-opus-4-8"]}
    Defaults to opus-4-7 + opus-4-8.

    Each canary prompt is sent to each model, judge scores each response
    on the new rubric (action_compliance + 4 anti-pattern tags), all rows
    share a comparison_id. Apples-to-apples. Returns the comparison_id;
    poll GET /v1/behavior/compare/latest for results.
    """
    import os as _os
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    models = ["anthropic/claude-opus-4-7", "anthropic/claude-opus-4-8"]
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("models"), list):
            models = [str(m) for m in body["models"] if isinstance(m, str) and m]
    except Exception:
        pass
    if len(models) < 1:
        raise HTTPException(status_code=400, detail="need at least one model")

    or_key = _os.environ.get("OPENROUTER_API_KEY", "")
    if not or_key:
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY not set in NautGate env",
        )

    from app.behavioral_canary import (
        run_comparison, quality_eval_config_or_default,
    )
    judge_config = await quality_eval_config_or_default(pool)
    judge_client = getattr(request.app.state, "quality_judge", None)
    try:
        comparison_id = await run_comparison(
            pool=pool,
            openrouter_api_key=or_key,
            judge_client=judge_client,
            judge_config=judge_config,
            models=models,
        )
    except Exception as exc:
        log.error("behavior_compare_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"comparison_failed: {exc}") from None
    return JSONResponse({
        "comparison_id": str(comparison_id),
        "models": models,
        "status": "completed",
    })


@router.get("/behavior/compare/latest")
async def behavior_compare_latest(request: Request) -> Response:
    """Latest comparison run grouped by canary name; one entry per model.

    Returned shape::

        {
          "comparison_id": "<uuid>",
          "ts": "ISO8601",
          "canaries": [
            {"name": "read_before_answer",
             "results": [
               {"target_model": "anthropic/claude-opus-4-7",
                "response_text": "...", "rubric": {...},
                "failure_tags": [...], "coach_notes": "..."},
               {"target_model": "anthropic/claude-opus-4-8", ...}
             ]},
            ...
          ]
        }
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.behavioral_canary import get_latest_comparison
    payload = await get_latest_comparison(pool)
    return JSONResponse(payload)


@router.get("/behavior/trace/{decision_id}")
async def behavior_trace(decision_id: str, request: Request) -> Response:
    """Full prompt-action trace for one decision.

    Returns the user prompt, the captured tool_calls_made sequence (in
    order), timing (first_byte_ms, duration_ms, reasoning_tokens), and
    the quality_eval verdict (rubric scores + failure tags + coach notes)
    if one exists. The dashboard renders this as the side-by-side
    "prompt → action" timeline.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad decision_id") from None
    row = await queries.get_behavior_trace(pool, decision_id=decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return JSONResponse(row)


@router.get("/whoami")
async def whoami(request: Request) -> Response:
    """Identity for the bearer token in this request.

    Returned: agent_id, default_profile, daily_budget_usd, key_id (last 4
    chars of the api key id for UI display). Used by the Sessions tab to
    label saved tokens.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)
    # Pull the api_key row so we can show the budget/profile alongside.
    row = await pool.fetchrow(
        """
        SELECT id::text AS key_id, default_profile, daily_budget_usd, last_used_at
          FROM nautgate.api_keys
         WHERE agent_id = $1
         ORDER BY last_used_at DESC NULLS LAST
         LIMIT 1
        """,
        agent_id,
    )
    return JSONResponse({
        "agent_id": agent_id,
        "key_id": row["key_id"] if row else None,
        "default_profile": row["default_profile"] if row else "auto",
        "daily_budget_usd": float(row["daily_budget_usd"]) if row and row["daily_budget_usd"] is not None else None,
        "last_used_at": row["last_used_at"].isoformat() if row and row["last_used_at"] else None,
    })


@router.get("/drift")
async def drift_overview(request: Request) -> Response:
    """Behavior-drift overview — open alerts + per-(provider, model, metric)
    baselines. Companion to /v1/scorecard.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from app.drift_engine import get_drift_overview
    return JSONResponse(await get_drift_overview(pool))


@router.get("/drift/{provider}/{model_path:path}/anomalies")
async def drift_anomalies(provider: str, model_path: str, request: Request) -> Response:
    """Recent anomaly events for one (provider, model, metric). model_path
    contains slashes hence path:; metric required as query param.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    metric = request.query_params.get("metric")
    if not metric:
        raise HTTPException(status_code=400, detail="metric query param required")
    try:
        limit = min(500, max(1, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50
    from app.drift_engine import get_recent_anomalies
    items = await get_recent_anomalies(
        pool, provider=provider, model=model_path, metric_name=metric, limit=limit,
    )
    return JSONResponse({"items": items, "count": len(items)})


# ── Drift Investigator ─────────────────────────────────────────────────────


@router.post("/drift/investigate")
async def drift_investigate(request: Request) -> Response:
    """Trigger a drift investigation. Body: {alert_id, provider, model,
    metric_name, suite (optional)}. Returns the investigation_id once
    started — the actual canary suite runs asynchronously; the client
    polls GET /v1/drift/investigations/{id} until status='complete'.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    provider = body.get("provider")
    model = body.get("model")
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider + model required")
    alert_id_raw = body.get("alert_id")
    alert_id: uuid.UUID | None = None
    if alert_id_raw:
        try:
            alert_id = uuid.UUID(str(alert_id_raw))
        except ValueError:
            raise HTTPException(status_code=400, detail="bad alert_id") from None
    metric = body.get("metric_name")
    suite = body.get("suite")
    from app.drift_investigator import run_investigation
    iid = await run_investigation(
        pool, alert_id=alert_id, provider=provider, model=model,
        metric_name=metric, suite=suite, triggered_by="manual",
    )
    if iid is None:
        raise HTTPException(status_code=422, detail="investigation_skipped_or_unsupported")
    return JSONResponse({"investigation_id": str(iid)})


@router.get("/drift/investigations")
async def drift_investigations_list(request: Request) -> Response:
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        limit = min(100, max(1, int(request.query_params.get("limit", "30"))))
    except ValueError:
        limit = 30
    alert_id: uuid.UUID | None = None
    aid_raw = request.query_params.get("alert_id")
    if aid_raw:
        try:
            alert_id = uuid.UUID(aid_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad alert_id") from None
    from app.drift_investigator import list_investigations
    items = await list_investigations(pool, limit=limit, alert_id=alert_id)
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/drift/investigations/{investigation_id}")
async def drift_investigation_get(investigation_id: str, request: Request) -> Response:
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    try:
        iid = uuid.UUID(investigation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad investigation_id") from None
    from app.drift_investigator import get_investigation
    row = await get_investigation(pool, iid)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return JSONResponse(row)


@router.get("/drift/report.html")
async def drift_report_html(request: Request) -> Response:
    """Render the drift report as a standalone HTML page — opened in a
    new tab, screenshotted, attached to a tweet.

    Auth accepted via the standard Authorization header OR via a ?token=…
    query parameter (see auth._extract_token_from_request). The query
    fallback exists specifically for this endpoint: the operator opens it
    in a fresh browser tab and can't attach headers.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    from fastapi.responses import HTMLResponse as _HTMLResponse

    from app.drift_investigator import generate_report
    out = await generate_report(pool)
    return _HTMLResponse(out["html"])


@router.post("/drift/report")
async def drift_report(request: Request) -> Response:
    """Generate a one-page drift report across every model with detected
    drift activity. Designed for paste-into-blog/Twitter/Obsidian.

    Body (all optional):
      - force_rerun (bool): re-run canaries even if a fresh investigation exists
      - models (list[[provider, model]]): explicit override of which to probe
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if not isinstance(body, dict):
        body = {}
    force_rerun = bool(body.get("force_rerun", False))
    models_override: list[tuple[str, str]] | None = None
    if isinstance(body.get("models"), list):
        models_override = []
        for m in body["models"]:
            if isinstance(m, list | tuple) and len(m) == 2:
                models_override.append((str(m[0]), str(m[1])))
            elif isinstance(m, dict) and "provider" in m and "model" in m:
                models_override.append((str(m["provider"]), str(m["model"])))

    from app.drift_investigator import generate_report
    out = await generate_report(
        pool, force_rerun=force_rerun, models=models_override,
    )
    return JSONResponse(out)


@router.get("/cost/openrouter-balance")
async def openrouter_balance(request: Request) -> Response:
    """Live OpenRouter credit balance + projected burn rate.

    Pulls from https://openrouter.ai/api/v1/credits (returns total_credits
    and total_usage), computes remaining = total_credits - total_usage,
    then derives a 7-day average daily burn from local audit-log spend on
    any decision_provider beginning with 'openrouter' (or actual_provider
    when set by the upstream).
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    import os as _os
    api_key = _os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return JSONResponse(
            {"error": "OPENROUTER_API_KEY not configured on the gateway."},
            status_code=503,
        )

    client = getattr(request.app.state, "quality_judge", None)
    if client is None:
        raise HTTPException(status_code=503, detail="upstream_client_unavailable")
    try:
        resp = await client.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8.0,
        )
    except Exception as exc:
        return JSONResponse({"error": f"openrouter_unreachable: {exc}"}, status_code=502)
    if resp.status_code != 200:
        return JSONResponse(
            {"error": f"http_{resp.status_code}", "detail": resp.text[:200]},
            status_code=502,
        )
    data = resp.json().get("data") or {}
    total_credits = float(data.get("total_credits") or 0.0)
    total_usage = float(data.get("total_usage") or 0.0)
    remaining = max(0.0, total_credits - total_usage)

    async with pool.acquire() as conn:
        spend_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(o.cost_usd), 0)::FLOAT AS spend
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes  o ON o.decision_id = d.id
             WHERE d.ts > NOW() - INTERVAL '7 days'
               AND (d.decision_provider = 'openrouter'
                    OR o.actual_provider = 'openrouter')
            """,
        )
    spend_7d = float((spend_row or {}).get("spend") or 0.0)
    daily_burn = spend_7d / 7.0 if spend_7d else None
    days_left = (
        remaining / daily_burn
        if daily_burn and daily_burn > 0 and remaining > 0
        else None
    )
    return JSONResponse({
        "total_credits": total_credits,
        "total_usage": total_usage,
        "remaining_usd": remaining,
        "spend_7d_usd": spend_7d,
        "daily_burn_usd": daily_burn,
        "days_left_at_current_burn": days_left,
    })


@router.get("/notifications")
async def notifications(request: Request) -> Response:
    """Cross-tab notification feed for the global header strip.

    Surfaces things the operator should know about regardless of which tab
    they're on — open drift alerts, subscription savings, rate-limit events
    today, daily-budget warnings. Polled every ~60s by the dashboard.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    await authenticate(pool, request)

    items: list[dict] = []
    async with pool.acquire() as conn:
        # Open drift alerts — counted ex-compaction since those are routine.
        alerts_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE metric_name <> 'messages_count_delta') AS real_drift,
              COUNT(*) FILTER (WHERE metric_name  = 'messages_count_delta') AS compactions
              FROM nautgate.drift_alerts
             WHERE resolved_at IS NULL
            """,
        )
        # Today's spend + savings (Europe/Berlin so it matches the dashboard).
        today_row = await conn.fetchrow(
            """
            SELECT
              COALESCE(SUM(o.cost_usd), 0)::FLOAT          AS metered,
              COALESCE(SUM(o.notional_cost_usd), 0)::FLOAT AS saved,
              SUM(CASE WHEN o.rate_limited_429 THEN 1 ELSE 0 END) AS rate_limited
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes  o ON o.decision_id = d.id
             WHERE date(d.ts AT TIME ZONE 'Europe/Berlin') = (CURRENT_DATE AT TIME ZONE 'Europe/Berlin')::date
            """,
        )

    real_drift = int((alerts_row or {}).get("real_drift") or 0)
    compactions = int((alerts_row or {}).get("compactions") or 0)
    if real_drift > 0:
        items.append({
            "level": "warning",
            "text": (f"{real_drift} open drift alert{'s' if real_drift != 1 else ''} — "
                     "click to investigate"),
            "href": "#drift",
        })
    elif compactions > 0:
        # Routine but worth surfacing so the operator knows the detector
        # is actually running. Lower priority than real drift.
        items.append({
            "level": "info",
            "text": (f"{compactions} active compaction event{'s' if compactions != 1 else ''} "
                     "(routine — long sessions auto-compacting)"),
            "href": "#drift",
        })

    metered = float((today_row or {}).get("metered") or 0.0)
    saved = float((today_row or {}).get("saved") or 0.0)
    if saved > 1.00:
        items.append({
            "level": "success",
            "text": f"Subscription saved ${saved:.2f} today",
            "href": "#cost",
        })
    # Daily budget alert: hardcoded $50/day informational threshold for now.
    # Real per-scope budgets land when budgets.py is wired up.
    if metered > 50.0:
        items.append({
            "level": "warning",
            "text": f"Today's metered spend is ${metered:.2f}",
            "href": "#cost",
        })

    rate_limited = int((today_row or {}).get("rate_limited") or 0)
    if rate_limited > 0:
        items.append({
            "level": "info",
            "text": (f"{rate_limited} rate-limit (429) event{'s' if rate_limited != 1 else ''} "
                     "today — subscription cap hit"),
            "href": "#cost",
        })

    return JSONResponse({"items": items})


@router.get("/stats")
async def stats(request: Request) -> Response:
    """Aggregate stats for the authenticated agent over the last `hours` (default 24).

    Reads route_decisions + route_outcomes; all counts default to 0 when empty.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)

    try:
        hours = int(request.query_params.get("hours", "24"))
    except ValueError:
        raise HTTPException(status_code=400, detail="hours must be an integer") from None
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be in 1..720")

    body = await queries.get_stats(pool, agent_id=agent_id, hours=hours)
    return JSONResponse(body)


@router.get("/profile")
async def get_profile(request: Request) -> Response:
    """Return the authenticated agent's routing_preferences, or empty defaults."""
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)
    return JSONResponse(await queries.get_routing_preferences(pool, agent_id=agent_id))


@router.put("/profile")
async def put_profile(request: Request) -> Response:
    """UPSERT the authenticated agent's routing_preferences.

    Accepts any subset of:
        preferred_tier_overrides (object), banned_models (string list),
        preferred_models (string list), notes (string).
    Unknown fields are ignored. agent_id is taken from the bearer token, never
    the body, so callers can't update someone else's profile.
    """
    pool = getattr(request.app.state, "db", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    agent_id = await authenticate(pool, request)

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    def _list_or_none(v):
        if v is None:
            return None
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise HTTPException(status_code=400, detail="must be a list of strings")
        return v

    overrides = body.get("preferred_tier_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise HTTPException(status_code=400, detail="preferred_tier_overrides must be an object")

    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(status_code=400, detail="notes must be a string")

    result = await queries.upsert_routing_preferences(
        pool,
        agent_id=agent_id,
        preferred_tier_overrides=overrides,
        banned_models=_list_or_none(body.get("banned_models")),
        preferred_models=_list_or_none(body.get("preferred_models")),
        notes=notes,
    )
    return JSONResponse(result)
