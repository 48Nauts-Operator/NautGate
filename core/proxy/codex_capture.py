"""mitmproxy addon — capture Codex (ChatGPT OAuth/subscription) traffic.

Current Codex (>= v0.142.x) ignores OPENAI_BASE_URL in ChatGPT-OAuth mode and
pins model traffic to chatgpt.com, so the old base-URL redirect (codexps /
FlowAI flow-proxy) no longer routes through NautGate. Codex DOES honour
HTTPS_PROXY + a trusted CA (it reads NODE_EXTRA_CA_CERTS / SSL_CERT_FILE /
CODEX_CA_CERTIFICATE), so we sit in front as a TLS-terminating forward proxy and
TEE the codex responses call into NautGate's existing recorder. mitmproxy still
forwards to chatgpt.com itself — we only observe, then write the audit row.

Reuses NautGate's own writers (queries.precapture, persist_outcome,
_extract_codex_usage) against the same Postgres, so the dashboard sees Codex
exactly like the (never-reached) /v1/responses OAuth forwarder intended.

Run:  uv run mitmdump -s app/../proxy/codex_capture.py --listen-port 8092
(from core/, so `app` is importable and mitmproxy is in the env).

ponytail: mitmproxy buffers the response before this hook, so Codex's stream is
delivered in one shot (slightly laggy UX) — fine for capture. Switch to a
streamed-tee if the latency matters.
"""

import json
import os
import sys
import uuid

# mitmdump runs this file directly and does not put core/ on sys.path; add it so
# `app` (the NautGate package, one level up from proxy/) is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from mitmproxy import http

from app.capture import capture_prompt, capture_response, capture_tools
from app.db import queries
from app.outcome import persist_outcome
from app.scoring import score
from app.usage import cache_prefix_hash, normalize_usage

_DB_URL = os.environ.get("NAUTGATE_DB_URL", "postgres://nautgate:nautgate@127.0.0.1:5432/nautgate")
# Only these are ours to record. Everything else passes through untouched.
_CODEX_HOST = "chatgpt.com"
_CODEX_PATH_PREFIX = "/backend-api/codex/responses"

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_DB_URL, min_size=1, max_size=4)
    return _pool


_DEBUG_DUMP = os.environ.get("CODEX_CAPTURE_DEBUG") == "1"


def _is_codex_responses(flow: http.HTTPFlow) -> bool:
    return flow.request.pretty_host == _CODEX_HOST and flow.request.path.startswith(
        _CODEX_PATH_PREFIX
    )


def _usage_from_response(resp: dict):
    """Map a Responses `response.usage` onto NormalizedUsage (cached subtracted)."""
    u = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(u, dict):
        return normalize_usage({}, provider_hint="openai")
    details = u.get("input_tokens_details")
    mapped = {
        "prompt_tokens": u.get("input_tokens") or u.get("prompt_tokens"),
        "completion_tokens": u.get("output_tokens") or u.get("completion_tokens"),
        "prompt_tokens_details": {
            "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None
        },
    }
    return normalize_usage(mapped, provider_hint="openai")


def _pair_turns(messages) -> list[tuple[dict, dict, float | None, float | None]]:
    """Pair each client `response.create` with the following server
    `response.completed`. Returns (request, response, start_ts, end_ts) per turn."""
    turns: list[tuple[dict, dict, float | None, float | None]] = []
    pending: dict | None = None
    start_ts: float | None = None
    for m in messages:
        try:
            ev = json.loads(m.content)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if m.from_client and t == "response.create":
            pending = ev
            start_ts = getattr(m, "timestamp", None)
        elif (not m.from_client) and t == "response.completed" and pending is not None:
            resp = ev.get("response")
            turns.append(
                (
                    pending,
                    resp if isinstance(resp, dict) else {},
                    start_ts,
                    getattr(m, "timestamp", None),
                )
            )
            pending = None
    return turns


async def websocket_end(flow: http.HTTPFlow) -> None:
    """Codex streams each model turn over a WebSocket to /backend-api/codex/responses.
    On close, record one NautGate row per response.create→response.completed pair."""
    if not _is_codex_responses(flow) or flow.websocket is None:
        return
    if _DEBUG_DUMP:
        with open("/tmp/codex_ws_dump.txt", "w") as f:
            for i, m in enumerate(flow.websocket.messages):
                who = "CLIENT" if m.from_client else "SERVER"
                f.write(
                    f"--- [{i}] {who} ({len(m.content)}b)\n"
                    f"{m.content.decode('utf-8', errors='replace')[:4000]}\n\n"
                )

    # Friendly fixed label for the dashboard session list. ponytail: single
    # GPT-MAX account → one name; switch to a map if multiple accounts appear.
    agent_id = "Bowden"
    turns = _pair_turns(flow.websocket.messages)
    if not turns:
        return
    try:
        pool = await _get_pool()
    except Exception as exc:
        print(f"[codex_capture] db pool failed: {exc}", flush=True)
        return
    src_ip = flow.client_conn.peername[0] if flow.client_conn.peername else None
    for req, resp, start_ts, end_ts in turns:
        try:
            await _record_turn(pool, agent_id, src_ip, req, resp, start_ts, end_ts)
        except Exception as exc:
            print(f"[codex_capture] record failed: {exc}", flush=True)


async def _record_turn(pool, agent_id, src_ip, req: dict, resp: dict, start_ts, end_ts) -> None:
    decision_id = uuid.uuid4()
    model = req.get("model")
    # Responses-API request carries `input` (items) + `instructions` (system) +
    # `tools`. Shape a payload the existing capture/score helpers understand.
    messages = req.get("input") if isinstance(req.get("input"), list) else None
    tools = req.get("tools") if isinstance(req.get("tools"), list) else None
    payload = {"model": model, "messages": messages, "tools": tools, "stream": True}

    captured_body = capture_prompt(messages, "none") if messages else None
    captured_tools = capture_tools(tools, "none") if tools else None
    req_size = len(json.dumps(req).encode("utf-8"))

    await queries.precapture(
        pool,
        decision_id=decision_id,
        agent_id=agent_id,
        inbound_format="openai_responses_ws",
        model_requested=model,
        classified_tier="passthrough",
        classified_score=score(payload).aggregate,
        classified_sensitivity="none",
        decision_provider="chatgpt-oauth",
        decision_model=model or "codex-default",
        decision_reason="chatgpt-oauth:ws-tee",
        prompt_body=captured_body.body if captured_body else None,
        prompt_body_truncated_at_byte=captured_body.truncated_at_byte if captured_body else None,
        tools_body=captured_tools.body if captured_tools else None,
        tools_body_truncated_at_byte=captured_tools.truncated_at_byte if captured_tools else None,
        source_ip=src_ip,
        messages_count=len(messages) if isinstance(messages, list) else None,
        tools_count=len(tools) if isinstance(tools, list) else None,
        stream_flag=True,
        request_size_bytes=req_size,
    )

    cu = _usage_from_response(resp)
    resp_json = json.dumps(resp)
    response_captured = capture_response(resp_json, "none")
    duration_ms = int((end_ts - start_ts) * 1000) if start_ts and end_ts else 0
    await persist_outcome(
        pool,
        None,
        decision_id=decision_id,
        status_code=200,
        duration_ms=duration_ms,
        first_byte_ms=None,
        upstream_overload_retries=0,
        prompt_tokens=cu.prompt_tokens,
        completion_tokens=cu.completion_tokens,
        reasoning_tokens=cu.reasoning_tokens,
        cache_read_tokens=cu.cache_read_tokens,
        cache_write_tokens=cu.cache_write_tokens,
        prefix_hash=cache_prefix_hash(payload),
        response_body=response_captured.body,
        response_body_truncated_at_byte=response_captured.truncated_at_byte,
        response_size_bytes=len(resp_json.encode("utf-8")),
        actual_provider="chatgpt-oauth",
        actual_model=model or "codex-subscription",
    )
    print(
        f"[codex_capture] recorded {agent_id} model={model} "
        f"in={cu.prompt_tokens} out={cu.completion_tokens}",
        flush=True,
    )
