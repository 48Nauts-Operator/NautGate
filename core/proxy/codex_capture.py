"""mitmproxy addon — nautproxy capture.

A forward proxy + trusted CA sits in front of clients that ignore
OPENAI_BASE_URL (today: Codex in ChatGPT-OAuth mode, which pins model traffic to
chatgpt.com but honours HTTPS_PROXY + NODE_EXTRA_CA_CERTS / SSL_CERT_FILE /
CODEX_CA_CERTIFICATE). We observe the turns and TEE them into NautGate's
``/v1/ingest`` endpoint — mitmproxy still forwards to the real upstream; we only
record.

Standalone by design: no ``app`` import, no DB access, just mitmproxy + stdlib.
It parses the wire bytes and POSTs JSON, so it ships as a compose sidecar with
only mitmproxy installed. The gateway's /v1/ingest does the capture/score/usage
normalization + attestation — same path inline traffic takes.

Env:
  NAUTGATE_INGEST_URL    where to POST turns (default http://nautgate:8090/v1/ingest)
  NAUTGATE_INGEST_TOKEN  shared secret sent as X-Ingest-Token (required; the
                         gateway disables /v1/ingest when its side is unset)
  NAUTPROXY_AGENT_ID     dashboard session label (default "codex")
  CODEX_CAPTURE_DEBUG=1  dump raw WS frames to /tmp/codex_ws_dump.txt

Run:  mitmdump -s proxy/codex_capture.py --listen-port 8092

ponytail: mitmproxy buffers the response before websocket_end, so Codex's stream
is delivered in one shot — fine for capture; switch to a streamed tee if the
observed latency ever matters.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

try:  # only needed at runtime under mitmdump; keep the module importable for tests
    from mitmproxy import http
except ImportError:  # pragma: no cover
    http = None  # type: ignore[assignment]

# Default to localhost — the common case is a local proxy in front of a local
# NautGate. The Docker sidecar overrides this with the compose service name
# (NAUTGATE_INGEST_URL=http://nautgate:8090/v1/ingest).
_INGEST_URL = os.environ.get("NAUTGATE_INGEST_URL", "http://localhost:8090/v1/ingest")
_INGEST_TOKEN = os.environ.get("NAUTGATE_INGEST_TOKEN", "")
_AGENT_ID = os.environ.get("NAUTPROXY_AGENT_ID", "codex")
_DEBUG_DUMP = os.environ.get("CODEX_CAPTURE_DEBUG") == "1"

# (host, path-prefix) → (inbound_format, provider). Only these are recorded;
# everything else passes through untouched. One row today (Codex). To capture
# another client that ignores base-URL but honours HTTPS_PROXY + a trusted CA,
# add a row here — the rest of the addon is provider-agnostic.
_CAPTURE = [
    ("chatgpt.com", "/backend-api/codex/responses", "openai_responses_ws", "chatgpt-oauth"),
]


def _match(host: str, path: str) -> tuple[str, str] | None:
    for h, prefix, fmt, provider in _CAPTURE:
        if host == h and path.startswith(prefix):
            return fmt, provider
    return None


def _usage_from_responses(resp: dict) -> dict:
    """Codex Responses ``usage`` → NautGate's normalized 5-count shape.

    input_tokens is the TOTAL; input_tokens_details.cached_tokens is a SUBSET, so
    fresh prompt = input_tokens - cached. (normalize_usage on the gateway reads
    input_tokens as already-fresh, so we do the subtraction here and hand it the
    finished counts, which /v1/ingest prefers over re-deriving from the body.)
    """
    u = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(u, dict):
        return {}
    idet = u.get("input_tokens_details")
    odet = u.get("output_tokens_details")
    cached = (idet.get("cached_tokens") or 0) if isinstance(idet, dict) else 0
    total_in = u.get("input_tokens") or u.get("prompt_tokens") or 0
    return {
        "prompt_tokens": max(total_in - cached, 0),
        "completion_tokens": u.get("output_tokens") or u.get("completion_tokens"),
        "reasoning_tokens": (odet.get("reasoning_tokens") if isinstance(odet, dict) else None),
        "cache_read_tokens": cached or None,
    }


def _consume(pending: dict, flow_id, content, from_client: bool, ts):
    """Fold one WS frame into per-flow state. Returns a completed
    (request, response, start_ts, end_ts) tuple when a server
    ``response.completed`` closes the pending client ``response.create`` — so
    each turn is emitted the instant it finishes, not at WS close. Else None.

    ponytail: one pending create per flow. Codex runs turns sequentially on a
    connection; if it ever pipelines, a second create overwrites the first.
    """
    try:
        ev = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if from_client and t == "response.create":
        pending[flow_id] = (ev, ts)
        return None
    if (not from_client) and t == "response.completed":
        p = pending.pop(flow_id, None)
        if p is None:
            return None
        create_ev, start_ts = p
        resp = ev.get("response")
        return create_ev, resp if isinstance(resp, dict) else {}, start_ts, ts
    return None


def _build_turn(agent_id, fmt, provider, src_ip, req, resp, start_ts, end_ts) -> dict:
    """Shape one paired turn into the /v1/ingest contract (pure — unit-tested)."""
    messages = req.get("input") if isinstance(req.get("input"), list) else None
    tools = req.get("tools") if isinstance(req.get("tools"), list) else None
    served = (resp.get("model") if isinstance(resp, dict) else None) or req.get("model")
    return {
        "agent_id": agent_id,
        "inbound_format": fmt,
        "provider": provider,
        "model": req.get("model"),
        "served_model": served,
        "messages": messages,
        "tools": tools,
        "response": resp,
        "usage": _usage_from_responses(resp),
        "stream": True,
        "status_code": 200,
        "duration_ms": int((end_ts - start_ts) * 1000) if start_ts and end_ts else 0,
        "source_ip": src_ip,
    }


# Bypass any HTTP(S)_PROXY the mitmproxy process inherited — otherwise the POST
# to NautGate gets routed back through this very proxy and loops.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_ingest(turn: dict) -> None:
    """POST one turn to /v1/ingest, direct (no proxy). Runs in a daemon thread,
    so it never blocks the mitmproxy event loop or touches its executor."""
    body = json.dumps(turn).encode("utf-8")
    req = urllib.request.Request(
        _INGEST_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-Ingest-Token": _INGEST_TOKEN},
        method="POST",
    )
    try:
        with _DIRECT.open(req, timeout=5) as r:
            r.read()
        print(
            f"[nautproxy] recorded {turn['agent_id']} model={turn['model']} "
            f"in={turn['usage'].get('prompt_tokens')} out={turn['usage'].get('completion_tokens')}",
            flush=True,
        )
    except urllib.error.HTTPError as exc:
        hint = " (set NAUTGATE_INGEST_TOKEN on both sides)" if exc.code in (401, 404) else ""
        print(f"[nautproxy] ingest {exc.code} → {_INGEST_URL}{hint}", flush=True)
    except Exception as exc:  # noqa: BLE001 — never let capture break the proxy
        print(f"[nautproxy] ingest failed → {_INGEST_URL}: {exc}", flush=True)


# Per-flow pending state (flow.id → (create_event, start_ts)).
_PENDING: dict = {}


def websocket_message(flow) -> None:
    """Fires on every WS frame. Record a turn the moment its response.completed
    arrives — so it appears LIVE in the dashboard, not only when the (long-lived,
    session-length) Codex WebSocket finally closes."""
    if flow.websocket is None:
        return
    matched = _match(flow.request.pretty_host, flow.request.path)
    if matched is None:
        return
    fmt, provider = matched
    m = flow.websocket.messages[-1]  # the frame that just arrived
    if _DEBUG_DUMP:
        who = "CLIENT" if m.from_client else "SERVER"
        with open("/tmp/codex_ws_dump.txt", "a") as f:
            f.write(
                f"--- {who} ({len(m.content)}b)\n{m.content.decode('utf-8', errors='replace')[:4000]}\n\n"
            )
    out = _consume(_PENDING, flow.id, m.content, m.from_client, getattr(m, "timestamp", None))
    if out is None:
        return
    req, resp, start_ts, end_ts = out
    src_ip = flow.client_conn.peername[0] if flow.client_conn.peername else None
    turn = _build_turn(_AGENT_ID, fmt, provider, src_ip, req, resp, start_ts, end_ts)
    threading.Thread(target=_post_ingest, args=(turn,), daemon=True).start()


def websocket_end(flow) -> None:
    """Drop any dangling pending state when the connection closes."""
    _PENDING.pop(getattr(flow, "id", None), None)
