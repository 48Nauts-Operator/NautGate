"""Quality & Prompt Coach analytics — LLM-as-judge post-outcome hook.

Mirrors the fire-and-forget pattern used by ``sb_memory`` and ``drift_engine``.
For each completed decision, we either sample (probability ``sample_rate``) or
detect an anomaly (was_empty, error, disconnect, bloat spike, …) and then
forward the captured prompt + response to a cheap judge model (default
``openai/gpt-4o-mini``). The judge returns structured JSON which we persist
to ``nautgate.quality_evals``.

Hard guarantees:
  • Never blocks the request path. Caller wraps this in try/except.
  • Honours the per-day cost cap. Skips when the cap is reached.
  • Honours the sensitivity gate — secret-classified prompts are never sent
    out to the judge model.
  • Returns silently when disabled, when bodies are missing, or when the
    judge call fails. No errors propagate to the caller.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import httpx
import structlog

from app.app_config import quality_eval_config
from app.db import queries

log = structlog.get_logger()

# ── Rubric prompt ───────────────────────────────────────────────────────────
# Cached automatically by OpenAI when the system prompt + first user turn
# exceeds 1024 tokens, so we keep this verbose on purpose. Same rubric for
# every call means high cache-hit rate.

RUBRIC_SYSTEM_PROMPT = """You are NautGate's quality auditor. You read one prompt/response pair from a real LLM call and rate the response on a fixed rubric. Your job is forensic: be specific about what went wrong, not generic.

You always respond with valid JSON matching this exact schema. Do not include any prose outside the JSON.

{
  "task_understanding":   0-5,   // Did the model grasp what was being asked? 5 = fully, 0 = totally missed it.
  "task_completion":      0-5,   // Did the model actually do the task? 5 = complete + correct, 3 = partial, 0 = nothing useful produced.
  "reasoning_efficiency": 0-5,   // Was the reasoning proportional to the task? 5 = tight, 3 = some bloat, 0 = thought forever then produced little.
  "prompt_clarity":       0-5,   // Was the user's prompt clear enough? 5 = unambiguous, 3 = needed inference, 0 = vague/missing context.
  "failure_tags":         [...], // Zero or more of: "looped", "hallucination", "off_task", "over_thinking", "under_thinking", "refusal", "partial_answer", "wrong_answer", "tool_misuse", "truncated", "multi_task_drop", "vague_scope". Empty array if the response was good.
  "anti_pattern":         "...", // What the user did WRONG in their prompt that caused this response, in ≤80 chars. Examples: "Asked for 3 things in one prompt without ordering", "Used 'check' without saying what to verify", "No success criteria provided". Empty string when the prompt was good and the model failed on its own.
  "suggested_prompt":     "...", // A concrete rewritten prompt the user SHOULD have sent. ≤300 chars. MANDATORY when task_completion < 4 OR prompt_clarity < 4 — produce a real rewrite, not "be clearer". Empty string ONLY when both scores are ≥ 4 (i.e. the prompt was fine).
  "coach_notes":          "..."  // 1-2 sentences explaining the scores. Be specific about what the model did or failed to do.
}

Scoring guidance:
- task_completion is the most important score. Be strict. A response that says "I'll help with that" but doesn't actually do the work is a 1, not a 3.
- Use "over_thinking" when the model used substantial reasoning tokens (you'll see this in the metadata) but produced thin or unfocused output.
- Use "looped" when the response restates the question, repeats itself, or stalls.
- Use "off_task" when the response addresses something other than what was asked.
- Use "hallucination" when the response asserts something demonstrably wrong about the code/API/tool/context provided.
- Use "multi_task_drop" when the user asked for N things and the model did <N — partial execution on multi-task prompts is the most common failure mode.
- Use "vague_scope" when the user asked something open-ended ("review this", "check this") without saying what specifically to look at.

suggested_prompt MUST be concrete and actionable. Bad: "Be more specific". Good: "Refactor only the auth() function in src/auth.py — keep the public signature, split internal logic into 3 helpers (parse, validate, persist). Return the diff only." If you can't think of a real rewrite, then `prompt_clarity` should be ≥ 4 and you can leave it empty.

anti_pattern should describe the SHAPE of the user's mistake in one short phrase, suitable for aggregating across many evals to find recurring habits. Match an existing pattern phrasing when one fits.

If the response is empty or the request errored, score everything 0 and add the appropriate failure_tag (e.g. "refusal" if the model declined, "truncated" if the response was cut off mid-sentence).

Only output the JSON object. No markdown fences, no explanation, no commentary."""


# ── Caches ──────────────────────────────────────────────────────────────────
# Mirror the sb_memory caching pattern: short TTL on config, longer on the
# daily spend total (which is a DB aggregate).

_config_cache: dict | None = None
_config_cached_at: float = 0.0
_CONFIG_CACHE_TTL_SEC = 10.0

_spend_cache_value: float = 0.0
_spend_cached_at: float = 0.0
_SPEND_CACHE_TTL_SEC = 60.0


async def _get_config(pool) -> dict:
    global _config_cache, _config_cached_at
    now = time.monotonic()
    if _config_cache is not None and (now - _config_cached_at) < _CONFIG_CACHE_TTL_SEC:
        return _config_cache
    cfg = await quality_eval_config(pool)
    _config_cache = cfg
    _config_cached_at = now
    return cfg


def config_cache_clear() -> None:
    """Force the next call to re-read config from the DB. Called by
    PUT /v1/config after a Settings save so changes take effect on the
    very next request.
    """
    global _config_cache, _config_cached_at, _spend_cache_value, _spend_cached_at
    _config_cache = None
    _config_cached_at = 0.0
    _spend_cache_value = 0.0
    _spend_cached_at = 0.0


async def _get_daily_spend(pool) -> float:
    global _spend_cache_value, _spend_cached_at
    now = time.monotonic()
    if (now - _spend_cached_at) < _SPEND_CACHE_TTL_SEC:
        return _spend_cache_value
    try:
        spend = await queries.get_daily_judge_spend(pool)
    except Exception as exc:
        log.warning("quality_eval_spend_lookup_failed", error=str(exc))
        return _spend_cache_value
    _spend_cache_value = spend
    _spend_cached_at = now
    return spend


def _bump_spend(amount: float) -> None:
    """Optimistically update the cached spend so the cap kicks in immediately
    after a high-cost eval instead of waiting for the cache to expire."""
    global _spend_cache_value
    _spend_cache_value += amount


# ── Trigger logic ───────────────────────────────────────────────────────────


def should_evaluate(
    decision: dict, outcome: dict, config: dict,
) -> tuple[bool, str]:
    """Decide whether to send this (decision, outcome) pair to the judge.

    Returns (True, "anomaly:<reason>") or (True, "sample") or (False, "").
    Anomalies always evaluate; non-anomalies evaluate at the configured rate.
    """
    if not config.get("enabled", True):
        return (False, "disabled")
    if (decision.get("classified_sensitivity") or "").lower() == "secret":
        return (False, "sensitive")

    # Anomaly triggers — anything we'd want to investigate.
    if outcome.get("was_empty"):
        return (True, "anomaly:was_empty")
    status = outcome.get("status_code")
    if isinstance(status, int) and status >= 400:
        return (True, f"anomaly:status_{status}")
    if outcome.get("client_disconnected"):
        return (True, "anomaly:client_disconnected")
    if outcome.get("was_truncated"):
        return (True, "anomaly:truncated")
    bloat = decision.get("bloat_score")
    if bloat is not None and float(bloat) >= 0.6:
        return (True, "anomaly:bloat")
    tools_count = decision.get("tools_count") or 0
    tool_calls = outcome.get("tool_calls_made")
    if tools_count > 0 and not tool_calls:
        return (True, "anomaly:no_tool_calls")

    sample_rate = float(config.get("sample_rate", 0.10) or 0.0)
    if sample_rate > 0 and random.random() < sample_rate:
        return (True, "sample")
    return (False, "")


# ── Judge call ──────────────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Some models still wrap JSON in ```json fences even when told not to."""
    return _FENCE_RE.sub("", text).strip()


def _make_user_message(decision: dict, outcome: dict) -> str:
    """Assemble the judge's user-turn from captured bodies + key metadata."""
    pb = (decision.get("prompt_body") or decision.get("prompt_excerpt") or "")[:8000]
    rb = (outcome.get("response_body") or "")[:8000]
    meta = {
        "model": decision.get("decision_model"),
        "provider": decision.get("decision_provider"),
        "tier": decision.get("classified_tier"),
        "classified_score": float(decision["classified_score"])
            if decision.get("classified_score") is not None else None,
        "prompt_tokens": outcome.get("prompt_tokens"),
        "completion_tokens": outcome.get("completion_tokens"),
        "reasoning_tokens": outcome.get("reasoning_tokens"),
        "duration_ms": outcome.get("duration_ms"),
        "was_empty": outcome.get("was_empty"),
        "was_truncated": outcome.get("was_truncated"),
        "client_disconnected": outcome.get("client_disconnected"),
        "tools_count": decision.get("tools_count") or 0,
        "tool_calls_made": len(outcome.get("tool_calls_made") or []),
    }
    return (
        f"### Call metadata\n{json.dumps(meta, indent=2)}\n\n"
        f"### User prompt (captured)\n{pb}\n\n"
        f"### Model response (captured)\n{rb}\n\n"
        f"Return the JSON rubric now."
    )


async def _call_judge(
    client: httpx.AsyncClient,
    config: dict,
    decision: dict,
    outcome: dict,
) -> tuple[dict | None, dict]:
    """Returns (parsed_rubric, telemetry). Telemetry always populated.

    Telemetry keys: judge_latency_ms, judge_cost_usd (None if pricing miss),
    judge_provider, judge_model, prompt_tokens, completion_tokens.
    """
    started = time.monotonic()
    telemetry: dict[str, Any] = {
        "judge_provider": config.get("judge_provider"),
        "judge_model": config.get("judge_model"),
        "judge_latency_ms": None,
        "judge_cost_usd": None,
        "prompt_tokens": None,
        "completion_tokens": None,
    }

    api_key = config.get("api_key") or ""
    base_url = (config.get("judge_base_url") or "https://openrouter.ai/api").rstrip("/")
    # Be lenient if the operator put a /v1 in their base_url (LMStudio's
    # default LMSTUDIO_BASE_URL does this). Always end up with one /v1.
    if base_url.endswith("/v1"):
        chat_url = f"{base_url}/chat/completions"
    else:
        chat_url = f"{base_url}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": config.get("judge_model"),
        "messages": [
            {"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
            {"role": "user", "content": _make_user_message(decision, outcome)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
    }

    try:
        resp = await client.post(chat_url, json=body, headers=headers, timeout=10.0)
        telemetry["judge_latency_ms"] = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            log.warning("quality_eval_judge_http_error",
                        status=resp.status_code, body=resp.text[:400])
            return None, telemetry
        payload = resp.json()
    except Exception as exc:
        telemetry["judge_latency_ms"] = int((time.monotonic() - started) * 1000)
        log.warning("quality_eval_judge_call_failed", error=str(exc))
        return None, telemetry

    choices = payload.get("choices") or []
    if not choices:
        return None, telemetry
    content = (choices[0].get("message") or {}).get("content") or ""
    try:
        rubric = json.loads(_strip_fences(content))
    except (ValueError, TypeError):
        log.warning("quality_eval_judge_bad_json", snippet=content[:200])
        return None, telemetry

    usage = payload.get("usage") or {}
    telemetry["prompt_tokens"] = usage.get("prompt_tokens")
    telemetry["completion_tokens"] = usage.get("completion_tokens")
    return rubric, telemetry


# ── Persistence + entry points ──────────────────────────────────────────────


async def _persist(
    pool, *, decision_id, rubric: dict, trigger: str, telemetry: dict,
    pricing,
) -> None:
    cost = telemetry.get("judge_cost_usd")
    if cost is None and pricing is not None:
        cost = pricing.compute_cost(
            telemetry.get("judge_provider"),
            telemetry.get("judge_model"),
            prompt_tokens=telemetry.get("prompt_tokens"),
            completion_tokens=telemetry.get("completion_tokens"),
        )
    if cost is not None:
        _bump_spend(float(cost))
    failure_tags = rubric.get("failure_tags") if isinstance(rubric, dict) else None
    if isinstance(failure_tags, list):
        failure_tags = [str(t) for t in failure_tags]
    else:
        failure_tags = []
    # Keep only the 4 numeric scores in `rubric` JSON so the column shape is
    # stable; suggested_prompt + coach_notes are top-level columns.
    rubric_payload = {
        k: rubric.get(k) for k in
        ("task_understanding", "task_completion", "reasoning_efficiency", "prompt_clarity")
        if isinstance(rubric, dict)
    }
    suggested = rubric.get("suggested_prompt") if isinstance(rubric, dict) else None
    notes = rubric.get("coach_notes") if isinstance(rubric, dict) else None
    anti = rubric.get("anti_pattern") if isinstance(rubric, dict) else None
    await queries.insert_quality_eval(
        pool,
        decision_id=decision_id,
        judge_provider=telemetry.get("judge_provider") or "",
        judge_model=telemetry.get("judge_model") or "",
        judge_cost_usd=cost,
        judge_latency_ms=telemetry.get("judge_latency_ms"),
        rubric=rubric_payload,
        failure_tags=failure_tags,
        suggested_prompt=(suggested or None) if isinstance(suggested, str) else None,
        coach_notes=(notes or None) if isinstance(notes, str) else None,
        trigger=trigger,
        anti_pattern=(anti or None) if isinstance(anti, str) else None,
    )


async def _load_pair(pool, decision_id) -> tuple[dict | None, dict | None]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id AS decision_id, d.decision_model, d.decision_provider,
                   d.classified_tier, d.classified_score, d.classified_sensitivity,
                   d.prompt_body, d.prompt_excerpt, d.tools_count, d.bloat_score,
                   o.status_code, o.was_empty, o.was_truncated,
                   o.client_disconnected, o.prompt_tokens, o.completion_tokens,
                   o.reasoning_tokens, o.duration_ms, o.response_body,
                   o.tool_calls_made
              FROM nautgate.route_decisions d
              LEFT JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE d.id = $1
            """,
            decision_id,
        )
    if row is None:
        return (None, None)
    decision = {
        "decision_id": row["decision_id"],
        "decision_model": row["decision_model"],
        "decision_provider": row["decision_provider"],
        "classified_tier": row["classified_tier"],
        "classified_score": row["classified_score"],
        "classified_sensitivity": row["classified_sensitivity"],
        "prompt_body": row["prompt_body"],
        "prompt_excerpt": row["prompt_excerpt"],
        "tools_count": row["tools_count"],
        "bloat_score": row["bloat_score"],
    }
    outcome = {
        "status_code": row["status_code"],
        "was_empty": row["was_empty"],
        "was_truncated": row["was_truncated"],
        "client_disconnected": row["client_disconnected"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "duration_ms": row["duration_ms"],
        "response_body": row["response_body"],
        "tool_calls_made": row["tool_calls_made"],
    }
    return decision, outcome


async def process_quality(
    pool, *, decision_id, judge_client: httpx.AsyncClient | None = None,
    pricing=None,
) -> None:
    """Post-outcome hook entry point. Mirrors process_drift / process_brain.

    Called fire-and-forget after every completed decision. Caller wraps in
    try/except; this function also catches everything internally so it never
    raises.
    """
    if pool is None or judge_client is None:
        return
    try:
        config = await _get_config(pool)
        if not config.get("enabled", True):
            return
        decision, outcome = await _load_pair(pool, decision_id)
        if decision is None or outcome is None:
            return
        ok, trigger = should_evaluate(decision, outcome, config)
        if not ok:
            return
        # Need at least *some* content to judge against.
        if not (decision.get("prompt_body") or decision.get("prompt_excerpt")):
            return
        if not outcome.get("response_body") and not outcome.get("was_empty"):
            return
        # Daily cap check.
        cap = float(config.get("daily_cost_cap_usd") or 0.0)
        if cap > 0:
            spent = await _get_daily_spend(pool)
            if spent >= cap:
                log.info("quality_eval_daily_cap_hit", spent=spent, cap=cap)
                return
        rubric, telemetry = await _call_judge(judge_client, config, decision, outcome)
        if rubric is None:
            return
        await _persist(
            pool, decision_id=decision["decision_id"],
            rubric=rubric, trigger=trigger, telemetry=telemetry, pricing=pricing,
        )
        log.info("quality_eval_written",
                 decision_id=str(decision["decision_id"]), trigger=trigger,
                 cost_usd=telemetry.get("judge_cost_usd"))
    except Exception as exc:
        log.warning("quality_eval_failed", error=str(exc),
                    decision_id=str(decision_id))


async def manual_evaluate(
    pool, *, decision_id, judge_client: httpx.AsyncClient | None,
    pricing=None, trigger: str = "manual",
) -> dict | None:
    """Bypass sampling — used by the Audit drawer's [Run eval] button and
    the 👎 thumbs-down icon. Still honours the sensitivity gate + daily cap.
    Returns the persisted eval row (or None on any failure).
    """
    if pool is None or judge_client is None:
        return None
    config = await _get_config(pool)
    if not config.get("enabled", True):
        return None
    decision, outcome = await _load_pair(pool, decision_id)
    if decision is None or outcome is None:
        return None
    if (decision.get("classified_sensitivity") or "").lower() == "secret":
        return None
    if not (decision.get("prompt_body") or decision.get("prompt_excerpt")):
        return None
    cap = float(config.get("daily_cost_cap_usd") or 0.0)
    if cap > 0:
        spent = await _get_daily_spend(pool)
        if spent >= cap:
            log.info("quality_eval_daily_cap_hit", spent=spent, cap=cap)
            return None
    rubric, telemetry = await _call_judge(judge_client, config, decision, outcome)
    if rubric is None:
        return None
    await _persist(
        pool, decision_id=decision["decision_id"],
        rubric=rubric, trigger=trigger, telemetry=telemetry, pricing=pricing,
    )
    return await queries.get_quality_eval(pool, decision_id)
