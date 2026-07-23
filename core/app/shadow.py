"""Champion–challenger shadow testing.

For a sampled slice of real traffic, re-run the SAME prompt on a cheaper
challenger model in the background, then have the judge blind-compare the two
answers (it never learns which model wrote which). Verdicts accumulate into
paired evidence: "challenger was as good or better in X% of N real prompts,
at Y% of the cost."

Hard guarantees (mirrors quality_eval):
  • Fire-and-forget after the outcome — never touches the response path.
  • Sensitivity gate: secret-classified prompts are never mirrored.
  • Scope gate: only tool-free calls with a captured prompt + a non-empty
    champion answer are eligible — agentic tool_use turns can't be compared
    fairly, so we don't pretend to.
  • Daily cost cap over challenger + judge spend combined.
  • All failures are swallowed and logged; a broken trial is verdict='error'.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any

import httpx
import structlog

from app.app_config import get_settings
from app.drift_investigator import _select_transports
from app.quality_eval import _load_pair, _readable_response, _strip_fences, is_machine_probe

log = structlog.get_logger()

CHALLENGER_MAX_TOKENS = 1000  # bound the mirror call; judge compares substance, not length

# ── Config ──────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "enabled": False,
    "sample_rate": 0.10,
    "challenger_provider": "openrouter",
    "challenger_model": "openrouter/openai/gpt-4o-mini",
    "daily_cost_cap_usd": 2.00,
    "max_prompt_bytes": 131072,
    # Prompt-diet trials: same model, pruned prompt. diet_apply maps
    # agent_id → strategy for agents where the proven diet runs in-flight.
    "diet_enabled": False,
    "diet_strategy": "history-6",
    "diet_apply": {},
}


def _pricing_provider(provider: str, model: str) -> str:
    """Passthrough claude traffic prices under the 'anthropic' key — same
    convention as anthropic_oauth_forwarder._notional_cost."""
    if provider == "passthrough" and "claude" in (model or "").lower():
        return "anthropic"
    return provider


async def shadow_config(pool) -> dict:
    settings = await get_settings(pool)
    cfg = dict(_DEFAULTS)
    cfg.update({k: v for k, v in (settings.get("shadow") or {}).items() if v is not None})
    return cfg


# The in-flight diet check runs on the REQUEST path, so it gets a short cache
# instead of a per-call DB read. PUT /v1/shadow/config clears it.
_diet_cache: tuple[float, dict] | None = None
_DIET_CACHE_TTL_SEC = 10.0


async def diet_apply_map(pool) -> dict:
    global _diet_cache
    now = time.monotonic()
    if _diet_cache is not None and (now - _diet_cache[0]) < _DIET_CACHE_TTL_SEC:
        return _diet_cache[1]
    cfg = await shadow_config(pool)
    m = dict(cfg.get("diet_apply") or {})
    _diet_cache = (now, m)
    return m


def diet_cache_clear() -> None:
    global _diet_cache
    _diet_cache = None


# ── Prompt + response shaping ───────────────────────────────────────────────


def flatten_messages(prompt_body: str | None) -> list[dict] | None:
    """Captured prompt_body → plain OpenAI-shape [{role, content:str}] messages.

    Content blocks are flattened to their text; anything non-text (images,
    tool_use, tool_result) disqualifies the call — return None so the caller
    skips it rather than mirroring a prompt the challenger can't actually see.
    """
    if not prompt_body:
        return None
    try:
        data = json.loads(prompt_body)
    except (ValueError, TypeError):
        return None
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list) or not msgs:
        return None
    out: list[dict] = []
    for m in msgs:
        if not isinstance(m, dict):
            return None
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            return None
        c = m.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            parts = []
            for p in c:
                if not (isinstance(p, dict) and p.get("type") == "text"):
                    return None  # non-text block → not fairly mirrorable
                parts.append(p.get("text") or "")
            text = "\n".join(parts)
        else:
            return None
        out.append({"role": role, "content": text})
    return out


# ── Challenger call ─────────────────────────────────────────────────────────


_CLAUDE_MINOR_RE = re.compile(r"-(\d)-(\d+)$")


def openrouter_claude_id(model: str) -> str:
    """Map a local Claude model name to its OpenRouter catalog id.
    claude-opus-4-8 → openrouter/anthropic/claude-opus-4.8 (minor versions use
    dots on OpenRouter); date-suffixed snapshots are stripped first."""
    base = re.sub(r"-20\d{6}$", "", model or "")
    base = _CLAUDE_MINOR_RE.sub(r"-\1.\2", base)
    return f"openrouter/anthropic/{base}"


async def call_challenger(
    client: httpx.AsyncClient,
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """One non-streaming completion on the challenger, with a self-healing leg:
    when a direct Anthropic call fails on credentials/billing (400/401/403) for
    a Claude model, retry once through OpenRouter — same weights, different
    biller. The result carries via_fallback so the trial is honest about it."""
    res = await _call_challenger_once(client, provider, model, messages, tools=tools)
    if (
        res.get("status") in (400, 401, 403)
        and not res.get("text")
        and provider in ("passthrough", "anthropic")
        and "claude" in (model or "").lower()
        and os.environ.get("OPENROUTER_API_KEY")
    ):
        alt = openrouter_claude_id(model)
        res2 = await _call_challenger_once(client, "openrouter", alt, messages, tools=tools)
        if res2.get("text"):
            res2["via_fallback"] = alt
            log.info("challenger_openrouter_fallback", model=model, alt=alt)
            return res2
    return res


def anthropic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI-format tool defs → Anthropic Messages format. Pure."""
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) and t.get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters")
                or fn.get("input_schema")
                or {"type": "object", "properties": {}},
            }
        )
    return out


async def _call_challenger_once(
    client: httpx.AsyncClient,
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """One non-streaming completion on the challenger. Returns
    {text, status, latency_ms, prompt_tokens, completion_tokens, tool_calls}
    (text None on error). Tool calls are CAPTURED, never executed."""
    transports = _select_transports(provider, model, prefer_oauth=True)
    if not transports:
        return {
            "text": None,
            "status": None,
            "latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "error": "no_transport",
        }
    t = transports[0]
    api_key = os.environ.get(t.api_key_env, "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        if t.auth_header.lower() == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    headers.update(t.extra_headers)

    if t.via in ("anthropic-oauth", "anthropic-metered"):
        url = f"{t.base_url}/v1/messages"
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        body: dict[str, Any] = {
            "model": model.removeprefix("openrouter/anthropic/"),
            "max_tokens": CHALLENGER_MAX_TOKENS,
            "messages": [m for m in messages if m["role"] != "system"],
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = anthropic_tools(tools)
    else:
        url = f"{t.base_url}/v1/chat/completions"
        body = {
            "model": model.removeprefix("openrouter/") if t.via == "openrouter" else model,
            "max_tokens": CHALLENGER_MAX_TOKENS,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

    started = time.monotonic()
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=30.0)
    except Exception as exc:
        return {
            "text": None,
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "prompt_tokens": None,
            "completion_tokens": None,
            "error": str(exc),
        }
    latency = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        return {
            "text": None,
            "status": resp.status_code,
            "latency_ms": latency,
            "prompt_tokens": None,
            "completion_tokens": None,
            "error": resp.text[:300],
        }
    try:
        payload = resp.json()
    except ValueError:
        return {
            "text": None,
            "status": resp.status_code,
            "latency_ms": latency,
            "prompt_tokens": None,
            "completion_tokens": None,
            "error": "bad_json",
        }
    text = None
    tool_calls: list[dict] = []
    usage = payload.get("usage") or {}
    if isinstance(payload.get("choices"), list) and payload["choices"]:
        msg = payload["choices"][0].get("message") or {}
        text = msg.get("content")
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if fn.get("name"):
                tool_calls.append(
                    {"name": fn["name"], "arguments": (fn.get("arguments") or "")[:400]}
                )
        pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    else:  # Anthropic Messages shape
        blocks = payload.get("content") or []
        text = (
            "\n".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
            or None
        )
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                tool_calls.append(
                    {"name": b["name"], "arguments": json.dumps(b.get("input") or {})[:400]}
                )
        pt, ct = usage.get("input_tokens"), usage.get("output_tokens")
    return {
        "text": text,
        "status": resp.status_code,
        "latency_ms": latency,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "tool_calls": tool_calls,
    }


# ── Blind paired judge ──────────────────────────────────────────────────────

_PAIR_RUBRIC = """You are a blind evaluator. You get one real user prompt and two answers, labeled Answer 1 and Answer 2. You do NOT know which model wrote which — judge only the content.

Pick the answer that better serves the user: correctness first, then completeness, then clarity. Length alone is not quality. If both are equally good OR equally bad, say tie.

Respond with ONLY this JSON, no prose:
{"winner": "1" | "2" | "tie", "reason": "<one sentence, ≤120 chars>"}"""


async def judge_pair(
    client: httpx.AsyncClient, judge_cfg: dict, prompt_text: str, answer_1: str, answer_2: str
) -> tuple[dict | None, dict]:
    """Blind comparison. Returns (verdict {winner, reason} | None, telemetry)."""
    telemetry: dict[str, Any] = {"prompt_tokens": None, "completion_tokens": None}
    base_url = (judge_cfg.get("judge_base_url") or "https://openrouter.ai/api").rstrip("/")
    chat_url = (
        f"{base_url}/chat/completions"
        if base_url.endswith("/v1")
        else f"{base_url}/v1/chat/completions"
    )
    headers = {"Content-Type": "application/json"}
    if judge_cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {judge_cfg['api_key']}"
    user = (
        f"### User prompt\n{prompt_text[:6000]}\n\n"
        f"### Answer 1\n{answer_1[:6000]}\n\n"
        f"### Answer 2\n{answer_2[:6000]}\n\nReturn the JSON now."
    )
    body = {
        "model": judge_cfg.get("judge_model"),
        "messages": [
            {"role": "system", "content": _PAIR_RUBRIC},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 120,
    }
    try:
        resp = await client.post(chat_url, json=body, headers=headers, timeout=15.0)
        if resp.status_code >= 400:
            log.warning("shadow_judge_http_error", status=resp.status_code)
            return None, telemetry
        payload = resp.json()
    except Exception as exc:
        log.warning("shadow_judge_call_failed", error=str(exc))
        return None, telemetry
    usage = payload.get("usage") or {}
    telemetry["prompt_tokens"] = usage.get("prompt_tokens")
    telemetry["completion_tokens"] = usage.get("completion_tokens")
    choices = payload.get("choices") or []
    if not choices:
        return None, telemetry
    try:
        verdict = json.loads(_strip_fences((choices[0].get("message") or {}).get("content") or ""))
    except (ValueError, TypeError):
        return None, telemetry
    if verdict.get("winner") not in ("1", "2", "tie"):
        return None, telemetry
    return verdict, telemetry


# ── Verdict math (pure) ─────────────────────────────────────────────────────


def shadow_verdict(verdicts: list[str], p0: float = 0.90) -> dict:
    """Non-inferiority summary. ok = challenger wins + ties. One-sided
    binomial test of H0: true ok-rate < p0, normal approximation with
    continuity correction. Pure."""
    import math

    counted = [v for v in verdicts if v in ("champion", "challenger", "tie")]
    n = len(counted)
    wins = sum(1 for v in counted if v == "challenger")
    ties = sum(1 for v in counted if v == "tie")
    losses = n - wins - ties
    ok = wins + ties
    out = {
        "n": n,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "ok_pct": round(ok / n, 3) if n else None,
        "p0": p0,
        "p_value": None,
        "non_inferior": None,
    }
    if n >= 10:
        z = (ok - n * p0 - 0.5) / math.sqrt(n * p0 * (1 - p0))
        p = 0.5 * math.erfc(z / math.sqrt(2))
        out["p_value"] = round(p, 4)
        out["non_inferior"] = bool(p < 0.05)
    return out


# ── Persistence + entry point ───────────────────────────────────────────────


async def _daily_spend(pool) -> float:
    row = await pool.fetchval(
        "SELECT COALESCE(SUM(COALESCE(challenger_cost_usd,0) + COALESCE(judge_cost_usd,0)), 0) "
        "FROM nautgate.shadow_trials WHERE ts > date_trunc('day', NOW())"
    )
    return float(row or 0)


async def _insert_trial(pool, **kw) -> None:
    await pool.execute(
        """
        INSERT INTO nautgate.shadow_trials
            (decision_id, champion_provider, champion_model,
             challenger_provider, challenger_model, challenger_response,
             challenger_status, challenger_latency_ms, challenger_cost_usd,
             champion_cost_usd, verdict, judge_reason, judge_cost_usd,
             trial_type, diet_strategy, original_bytes, pruned_bytes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        """,
        kw["decision_id"],
        kw["champion_provider"],
        kw["champion_model"],
        kw["challenger_provider"],
        kw["challenger_model"],
        kw.get("challenger_response"),
        kw.get("challenger_status"),
        kw.get("challenger_latency_ms"),
        kw.get("challenger_cost_usd"),
        kw.get("champion_cost_usd"),
        kw.get("verdict"),
        kw.get("judge_reason"),
        kw.get("judge_cost_usd"),
        kw.get("trial_type", "model"),
        kw.get("diet_strategy"),
        kw.get("original_bytes"),
        kw.get("pruned_bytes"),
    )


async def process_shadow(
    pool, *, decision_id, shadow_client: httpx.AsyncClient | None = None, pricing=None
) -> None:
    """Post-outcome hook. Fire-and-forget; never raises."""
    if pool is None or shadow_client is None:
        return
    try:
        cfg = await shadow_config(pool)
        if not (cfg.get("enabled") or cfg.get("diet_enabled")):
            return
        if random.random() >= float(cfg.get("sample_rate") or 0):
            return
        decision, outcome = await _load_pair(pool, decision_id)
        if decision is None or outcome is None:
            return
        # Eligibility gates — see module docstring.
        if (decision.get("classified_sensitivity") or "").lower() == "secret":
            return
        if is_machine_probe(decision):
            return
        if (decision.get("tools_count") or 0) > 0:
            return
        body = decision.get("prompt_body") or ""
        if not body or len(body) > int(cfg.get("max_prompt_bytes") or 131072):
            return
        champion_text = _readable_response(outcome.get("response_body") or "").strip()
        if not champion_text:
            return
        messages = flatten_messages(body)
        if not messages:
            return
        cap = float(cfg.get("daily_cost_cap_usd") or 0)
        if cap > 0 and await _daily_spend(pool) >= cap:
            log.info("shadow_daily_cap_hit", cap=cap)
            return

        # Which trial types are possible on this call? Model trials need the
        # configured challenger; prompt-diet needs the pruning to actually
        # remove something. When both are live, flip a coin — one trial per
        # call keeps cost accounting simple.
        from app.diet import payload_bytes, prune_messages

        champ_provider = decision.get("decision_provider") or "?"
        champ_model = decision.get("decision_model") or "?"
        options: list[str] = []
        if cfg.get("enabled"):
            options.append("model")
        diet_strategy = str(cfg.get("diet_strategy") or "history-6")
        pruned = prune_messages(messages, diet_strategy) if cfg.get("diet_enabled") else None
        if pruned is not None:
            options.append("prompt_diet")
        if not options:
            return
        trial_type = random.choice(options)

        extras: dict[str, Any] = {"trial_type": trial_type}
        if trial_type == "prompt_diet":
            ch_provider, ch_model = champ_provider, champ_model
            send_messages = pruned
            extras.update(
                diet_strategy=diet_strategy,
                original_bytes=payload_bytes(messages),
                pruned_bytes=payload_bytes(pruned),
            )
        else:
            ch_provider = cfg["challenger_provider"]
            ch_model = cfg["challenger_model"]
            send_messages = messages

        res = await call_challenger(shadow_client, ch_provider, ch_model, send_messages)
        ch_cost = None
        if pricing is not None and res.get("prompt_tokens") is not None:
            ch_cost = pricing.compute_cost(
                _pricing_provider(ch_provider, ch_model),
                ch_model,
                prompt_tokens=res["prompt_tokens"],
                completion_tokens=res["completion_tokens"],
            )
        champion_cost = outcome.get("notional_cost_usd") or outcome.get("cost_usd")

        trial = dict(
            decision_id=decision["decision_id"],
            champion_provider=champ_provider,
            champion_model=champ_model,
            challenger_provider=ch_provider,
            challenger_model=ch_model,
            challenger_response=res.get("text"),
            challenger_status=res.get("status"),
            challenger_latency_ms=res.get("latency_ms"),
            challenger_cost_usd=ch_cost,
            champion_cost_usd=champion_cost,
            **extras,
        )
        if not res.get("text"):
            trial.update(verdict="error", judge_reason=(res.get("error") or "empty_response")[:200])
            await _insert_trial(pool, **trial)
            return

        # Blind judge — randomize which answer is "1".
        from app.app_config import quality_eval_config

        judge_cfg = await quality_eval_config(pool)
        prompt_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        champ_first = random.random() < 0.5
        a1, a2 = (champion_text, res["text"]) if champ_first else (res["text"], champion_text)
        verdict, telem = await judge_pair(shadow_client, judge_cfg, prompt_text, a1, a2)
        if verdict is None:
            trial.update(verdict="error", judge_reason="judge_failed")
        else:
            w = verdict["winner"]
            mapped = (
                "tie" if w == "tie" else ("champion" if (w == "1") == champ_first else "challenger")
            )
            judge_cost = None
            if pricing is not None:
                judge_cost = pricing.compute_cost(
                    judge_cfg.get("judge_provider"),
                    judge_cfg.get("judge_model"),
                    prompt_tokens=telem.get("prompt_tokens"),
                    completion_tokens=telem.get("completion_tokens"),
                )
            trial.update(
                verdict=mapped,
                judge_reason=(verdict.get("reason") or "")[:300],
                judge_cost_usd=judge_cost,
            )
        await _insert_trial(pool, **trial)
        log.info(
            "shadow_trial_written",
            decision_id=str(decision_id),
            verdict=trial.get("verdict"),
            challenger=ch_model,
        )
    except Exception as exc:
        log.warning("shadow_trial_failed", error=str(exc), decision_id=str(decision_id))


# ── Prompt-improvement simulation (Improvements page) ──────────────────────


def replace_last_user(messages: list[dict], new_text: str) -> list[dict] | None:
    """Swap the LAST user message's content for the improved prompt. Pure.
    Returns None when there's no user message to replace."""
    if not isinstance(messages, list) or not new_text:
        return None
    idx = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            idx = i
            break
    if idx is None:
        return None
    out = [dict(m) for m in messages]
    out[idx]["content"] = new_text
    return out


async def simulate_improvement(
    pool, *, decision_id, client: httpx.AsyncClient, pricing=None
) -> dict:
    """On-demand: re-run this call with the judge's suggested_prompt in place
    of the user's last message, blind-judge both answers, persist as a
    'prompt_improve' shadow trial. Returns the trial result (or {'error': …}).
    Synchronous by design — the user clicked 'simulate' and is waiting."""
    if client is None:
        return {"error": "judge_client_unavailable"}
    eval_row = await pool.fetchrow(
        "SELECT suggested_prompt FROM nautgate.quality_evals WHERE decision_id = $1", decision_id
    )
    suggested = (eval_row["suggested_prompt"] if eval_row else None) or ""
    if not suggested.strip():
        return {"error": "no_suggested_prompt"}
    decision, outcome = await _load_pair(pool, decision_id)
    if decision is None or outcome is None:
        return {"error": "decision_not_found"}
    if (decision.get("classified_sensitivity") or "").lower() == "secret":
        return {"error": "sensitivity_gate"}
    champion_text = _readable_response(outcome.get("response_body") or "").strip()
    messages = flatten_messages(decision.get("prompt_body"))
    if not messages or not champion_text:
        return {"error": "not_mirrorable"}
    improved = replace_last_user(messages, suggested)
    if improved is None:
        return {"error": "no_user_message"}

    champ_provider = decision.get("decision_provider") or "?"
    champ_model = decision.get("decision_model") or "?"
    res = await call_challenger(client, champ_provider, champ_model, improved)
    ch_cost = None
    if pricing is not None and res.get("prompt_tokens") is not None:
        ch_cost = pricing.compute_cost(
            _pricing_provider(champ_provider, champ_model),
            champ_model,
            prompt_tokens=res["prompt_tokens"],
            completion_tokens=res["completion_tokens"],
        )
    trial = dict(
        decision_id=decision["decision_id"],
        champion_provider=champ_provider,
        champion_model=champ_model,
        challenger_provider=champ_provider,
        challenger_model=champ_model,
        challenger_response=res.get("text"),
        challenger_status=res.get("status"),
        challenger_latency_ms=res.get("latency_ms"),
        challenger_cost_usd=ch_cost,
        champion_cost_usd=outcome.get("notional_cost_usd") or outcome.get("cost_usd"),
        trial_type="prompt_improve",
    )
    if not res.get("text"):
        trial.update(verdict="error", judge_reason=(res.get("error") or "empty_response")[:200])
        await _insert_trial(pool, **trial)
        return {
            "verdict": "error",
            "detail": res.get("error"),
            "challenger_status": res.get("status"),
        }

    from app.app_config import quality_eval_config

    judge_cfg = await quality_eval_config(pool)
    prompt_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
    champ_first = random.random() < 0.5
    a1, a2 = (champion_text, res["text"]) if champ_first else (res["text"], champion_text)
    verdict, telem = await judge_pair(client, judge_cfg, prompt_text, a1, a2)
    if verdict is None:
        trial.update(verdict="error", judge_reason="judge_failed")
    else:
        w = verdict["winner"]
        mapped = (
            "tie" if w == "tie" else ("champion" if (w == "1") == champ_first else "challenger")
        )
        judge_cost = None
        if pricing is not None:
            judge_cost = pricing.compute_cost(
                judge_cfg.get("judge_provider"),
                judge_cfg.get("judge_model"),
                prompt_tokens=telem.get("prompt_tokens"),
                completion_tokens=telem.get("completion_tokens"),
            )
        trial.update(
            verdict=mapped,
            judge_reason=(verdict.get("reason") or "")[:300],
            judge_cost_usd=judge_cost,
        )
    await _insert_trial(pool, **trial)
    return {
        "verdict": trial.get("verdict"),
        "judge_reason": trial.get("judge_reason"),
        "improved_prompt": suggested,
        "original_response": champion_text[:4000],
        "improved_response": (res.get("text") or "")[:4000],
        "original_cost_usd": float(trial["champion_cost_usd"])
        if trial.get("champion_cost_usd") is not None
        else None,
        "improved_cost_usd": float(ch_cost) if ch_cost is not None else None,
        "improved_latency_ms": res.get("latency_ms"),
    }


# ── Read side for the dashboard ─────────────────────────────────────────────


async def summary(pool, days: int = 30) -> dict:
    rows = await pool.fetch(
        """
        SELECT champion_model, challenger_model, trial_type, diet_strategy,
               ARRAY_AGG(verdict) AS verdicts,
               AVG(champion_cost_usd) FILTER (WHERE verdict != 'error')::float   AS champ_avg_cost,
               AVG(challenger_cost_usd) FILTER (WHERE verdict != 'error')::float AS chall_avg_cost,
               AVG(challenger_latency_ms) FILTER (WHERE verdict != 'error')::float AS chall_avg_latency,
               AVG(1.0 - pruned_bytes::float / NULLIF(original_bytes, 0))
                   FILTER (WHERE verdict != 'error')::float AS avg_reduction,
               COUNT(*) FILTER (WHERE verdict = 'error') AS errors,
               MAX(ts) AS last_trial
          FROM nautgate.shadow_trials
         WHERE ts > NOW() - make_interval(days => $1)
         GROUP BY 1, 2, 3, 4
        """,
        days,
    )
    experiments = []
    for r in rows:
        v = shadow_verdict([x for x in (r["verdicts"] or []) if x])
        # Projected monthly saving: champion's 30d call volume at the per-call delta.
        volume = await pool.fetchval(
            """
            SELECT COUNT(*) FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.decision_model = $1 AND d.ts > NOW() - interval '30 days'
               AND o.status_code BETWEEN 200 AND 299
            """,
            r["champion_model"],
        )
        saving = None
        if r["champ_avg_cost"] is not None and r["chall_avg_cost"] is not None:
            saving = round((r["champ_avg_cost"] - r["chall_avg_cost"]) * int(volume or 0), 2)
        experiments.append(
            {
                "champion": r["champion_model"],
                "challenger": r["challenger_model"],
                "trial_type": r["trial_type"],
                "diet_strategy": r["diet_strategy"],
                "avg_reduction": round(r["avg_reduction"], 3)
                if r["avg_reduction"] is not None
                else None,
                **v,
                "errors": int(r["errors"]),
                "champ_avg_cost": r["champ_avg_cost"],
                "chall_avg_cost": r["chall_avg_cost"],
                "chall_avg_latency_ms": r["chall_avg_latency"],
                "monthly_volume": int(volume or 0),
                "projected_monthly_saving_usd": saving,
                "last_trial": r["last_trial"].isoformat() if r["last_trial"] else None,
            }
        )
    experiments.sort(key=lambda e: -(e["n"] or 0))
    trials = [
        dict(r)
        for r in await pool.fetch(
            """
        SELECT t.id::text, t.ts, t.champion_model, t.challenger_model, t.verdict,
               t.judge_reason, t.challenger_latency_ms, t.trial_type, t.diet_strategy,
               d.prompt_excerpt
          FROM nautgate.shadow_trials t
          JOIN nautgate.route_decisions d ON d.id = t.decision_id
         ORDER BY t.ts DESC LIMIT 20
        """
        )
    ]
    for t in trials:
        t["ts"] = t["ts"].isoformat()
    return {"days": days, "experiments": experiments, "recent_trials": trials}


async def trial_detail(pool, trial_id: str) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT t.id::text, t.ts, t.champion_model, t.challenger_model, t.verdict,
               t.judge_reason, t.challenger_response, t.challenger_latency_ms,
               t.challenger_status, t.champion_cost_usd, t.challenger_cost_usd,
               t.trial_type, t.diet_strategy, t.original_bytes, t.pruned_bytes,
               d.prompt_body, d.prompt_excerpt, d.id::text AS decision_id,
               o.response_body, o.duration_ms AS champion_latency_ms
          FROM nautgate.shadow_trials t
          JOIN nautgate.route_decisions d ON d.id = t.decision_id
          LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
         WHERE t.id::text = $1
        """,
        trial_id,
    )
    if row is None:
        return None
    d = dict(row)
    d["ts"] = d["ts"].isoformat()
    d["champion_response"] = _readable_response(d.pop("response_body") or "")
    msgs = flatten_messages(d.pop("prompt_body"))
    d["prompt_text"] = (
        "\n".join(m["content"] for m in msgs if m["role"] == "user")
        if msgs
        else (d.get("prompt_excerpt") or "")
    )
    for k in ("champion_cost_usd", "challenger_cost_usd"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return d
