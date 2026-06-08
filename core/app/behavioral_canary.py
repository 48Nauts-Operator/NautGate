"""Behavioral canary suite — apples-to-apples model comparison.

Runs a small set of prompts that EXPOSE cowboy behaviour (jump-to-action,
skip-the-doc, assume-instead-of-read) through two or more models via
OpenRouter, then scores each response with the new quality_eval rubric
(action_compliance + four agentic anti-pattern tags).

The canary prompts are self-contained: they include the "document" or
"code" the model is supposed to read INLINE, so we don't need a working
tool harness — the test is whether the model's response demonstrates that
it actually read what was provided vs. assumed and proceeded.

A run produces one row per (canary × model) in behavioral_canary_runs,
all tagged with the same comparison_id. The dashboard shows the latest
comparison_id's results side-by-side per canary.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

import httpx
import structlog

from app.app_config import quality_eval_config

log = structlog.get_logger()


@dataclass(frozen=True)
class BehavioralCanary:
    """One behavioural test prompt with the rubric expectation embedded
    in the prompt itself, so the judge can score compliance against the
    stated instructions.
    """
    name: str
    description: str
    prompt: str
    max_tokens: int = 800


# Five probes that map to specific cowboy symptoms.
# Each prompt is paired with a clear constraint the model is supposed to
# follow. action_compliance scores 5/5 when the response respects the
# constraint; drops as the model takes shortcuts.

BEHAVIORAL_CANARIES: tuple[BehavioralCanary, ...] = (
    BehavioralCanary(
        name="read_before_answer",
        description="Tests whether the model READS the supplied document before answering",
        prompt=(
            "Below is a configuration file (CONFIG.md). Your job:\n"
            "  1. Read it carefully.\n"
            "  2. Answer ONE question: what is the value of `default_timeout_ms`?\n"
            "Do NOT explain the file. Do NOT summarise anything else. "
            "Just answer the single question, or say 'NOT FOUND' if the key "
            "isn't there.\n\n"
            "--- CONFIG.md ---\n"
            "# Service config\n\n"
            "## HTTP\n"
            "- listen_port: 8090\n"
            "- max_connections: 1024\n"
            "- default_timeout_ms: 4500\n"
            "- keepalive_seconds: 60\n\n"
            "## Database\n"
            "- host: stargate.local\n"
            "- port: 5432\n"
            "--- END CONFIG.md ---\n"
        ),
    ),
    BehavioralCanary(
        name="respect_constraint",
        description="Tests whether the model honours an explicit DO NOT instruction",
        prompt=(
            "Here is a Python function. Your job: identify whether it has a "
            "bug.\n\n"
            "STRICT CONSTRAINT: only NAME the bug in one sentence. Do NOT "
            "suggest a fix. Do NOT rewrite the function. Do NOT propose "
            "alternatives. If you produce a fix, you have failed the task.\n\n"
            "```python\n"
            "def average(numbers):\n"
            "    return sum(numbers) / len(numbers)\n"
            "```\n"
        ),
    ),
    BehavioralCanary(
        name="investigate_before_claim",
        description="Tests whether the model checks first or asserts from assumption",
        prompt=(
            "Below is a 30-line snippet from auth_handler.py. A user reports "
            "that login fails for emails containing '+' (e.g. user+test@x.com). "
            "Your job: identify which specific line is responsible.\n\n"
            "Cite the line number. Do NOT propose a fix. Do NOT speculate "
            "about other possible causes. Cite the line you can see is the "
            "cause, or say 'not visible in this snippet' if the cause isn't "
            "in the code shown.\n\n"
            "```python\n"
            "01: import re\n"
            "02: from urllib.parse import quote\n"
            "03: \n"
            "04: EMAIL_RE = re.compile(r'^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$')\n"
            "05: \n"
            "06: def normalize_email(raw: str) -> str:\n"
            "07:     return raw.strip().lower()\n"
            "08: \n"
            "09: def is_valid_email(s: str) -> bool:\n"
            "10:     return bool(EMAIL_RE.match(s))\n"
            "11: \n"
            "12: def login(email: str, password: str) -> dict:\n"
            "13:     email = normalize_email(email)\n"
            "14:     if not is_valid_email(email):\n"
            "15:         return {'ok': False, 'error': 'invalid_email'}\n"
            "16:     row = db.fetchone('SELECT * FROM users WHERE email=?', (email,))\n"
            "17:     if not row:\n"
            "18:         return {'ok': False, 'error': 'unknown_user'}\n"
            "19:     if not verify_password(password, row['pw_hash']):\n"
            "20:         return {'ok': False, 'error': 'bad_password'}\n"
            "21:     return {'ok': True, 'session': new_session(row['id'])}\n"
            "```\n"
        ),
    ),
    BehavioralCanary(
        name="multi_step_order",
        description="Tests whether the model honours a numbered task sequence",
        prompt=(
            "Three things, IN ORDER. You must complete step N before moving "
            "to step N+1.\n\n"
            "STEP 1: Below is a string of numbers separated by commas. Count "
            "how many there are.\n"
            "STEP 2: List the numbers that are even.\n"
            "STEP 3: Sum the odd numbers.\n\n"
            "Reply with exactly three labelled lines: 'STEP 1: <count>', "
            "'STEP 2: <comma-separated evens>', 'STEP 3: <sum of odds>'. "
            "Nothing else.\n\n"
            "Numbers: 4, 9, 12, 7, 3, 18, 25, 30, 11, 6, 14, 1\n"
        ),
    ),
    BehavioralCanary(
        name="dont_assume",
        description="Tests whether the model asks rather than guesses",
        prompt=(
            "I want to deploy my app. I haven't told you the language, the "
            "host, the framework, or what 'deploy' means in my context. "
            "Your job: respond with EITHER a single clarifying question OR "
            "the phrase 'NEED CLARIFICATION'. Do NOT provide deployment "
            "instructions. Do NOT assume a stack. Do NOT list options.\n"
        ),
    ),
)


@dataclass
class CanaryRunResult:
    canary_name: str
    target_model: str
    prompt: str
    response_text: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int
    status_code: int | None
    error: str | None
    # Filled in after the judge runs:
    rubric: dict | None = None
    failure_tags: list[str] | None = None
    coach_notes: str | None = None
    judge_cost_usd: float | None = None


async def _call_openrouter(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> CanaryRunResult:
    """Send one prompt to one model via OpenRouter. No tools — pure
    text comparison; the prompt instructs the model how to behave and
    the response IS the test artifact.
    """
    started = time.monotonic()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # OpenRouter wants HTTP-Referer + X-Title for routing analytics —
        # tag these as NautGate behavioral runs so they're identifiable
        # in the OpenRouter dashboard.
        "HTTP-Referer": "https://nautgate.local",
        "X-Title": "NautGate behavioral canary",
    }
    try:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=body, headers=headers, timeout=60.0,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return CanaryRunResult(
            canary_name="",  # caller fills
            target_model=model,
            prompt=prompt,
            response_text=None,
            prompt_tokens=None,
            completion_tokens=None,
            duration_ms=duration_ms,
            status_code=None,
            error=f"transport: {exc}",
        )

    if resp.status_code >= 400:
        return CanaryRunResult(
            canary_name="", target_model=model, prompt=prompt,
            response_text=None, prompt_tokens=None, completion_tokens=None,
            duration_ms=duration_ms, status_code=resp.status_code,
            error=resp.text[:400],
        )
    try:
        payload = resp.json()
    except Exception as exc:
        return CanaryRunResult(
            canary_name="", target_model=model, prompt=prompt,
            response_text=None, prompt_tokens=None, completion_tokens=None,
            duration_ms=duration_ms, status_code=resp.status_code,
            error=f"bad_json: {exc}",
        )
    choices = payload.get("choices") or []
    if not choices:
        return CanaryRunResult(
            canary_name="", target_model=model, prompt=prompt,
            response_text=None, prompt_tokens=None, completion_tokens=None,
            duration_ms=duration_ms, status_code=resp.status_code,
            error="no_choices",
        )
    response_text = (choices[0].get("message") or {}).get("content") or ""
    usage = payload.get("usage") or {}
    return CanaryRunResult(
        canary_name="", target_model=model, prompt=prompt,
        response_text=response_text,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        duration_ms=duration_ms,
        status_code=resp.status_code,
        error=None,
    )


async def _judge_canary(
    judge_client: httpx.AsyncClient | None,
    judge_config: dict,
    canary: BehavioralCanary,
    result: CanaryRunResult,
) -> CanaryRunResult:
    """Run the new quality_eval rubric on one canary result.

    Reuses the rubric system prompt from app.quality_eval so scores are
    on the same scale as live audit-log evals. Stores rubric inline on
    the result (we don't insert into quality_evals because canary runs
    don't have route_decisions rows).
    """
    if judge_client is None or result.response_text is None:
        return result
    from app.quality_eval import RUBRIC_SYSTEM_PROMPT

    # Build a minimal user-message matching _make_user_message's shape,
    # but for a canary (no tool sequence — pure text test).
    user_msg = (
        f"### Call metadata\n"
        f"{json.dumps({'model': result.target_model, 'provider': 'openrouter', 'tier': 'behavioral_canary', 'prompt_tokens': result.prompt_tokens, 'completion_tokens': result.completion_tokens, 'duration_ms': result.duration_ms, 'tools_count': 0, 'tool_calls_made_count': 0, 'tool_sequence': []}, indent=2)}\n\n"
        f"### Canary: {canary.name} — {canary.description}\n\n"
        f"### User prompt (captured)\n{result.prompt}\n\n"
        f"### Model response (captured)\n{result.response_text}\n\n"
        f"Return the JSON rubric now."
    )
    api_key = judge_config.get("api_key") or ""
    base_url = (judge_config.get("judge_base_url") or "https://openrouter.ai/api").rstrip("/")
    chat_url = (
        f"{base_url}/chat/completions" if base_url.endswith("/v1")
        else f"{base_url}/v1/chat/completions"
    )
    body = {
        "model": judge_config.get("judge_model"),
        "messages": [
            {"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        jr = await judge_client.post(chat_url, json=body, headers=headers, timeout=10.0)
        if jr.status_code >= 400:
            log.warning("behavioral_judge_http_error", status=jr.status_code, body=jr.text[:200])
            return result
        jpayload = jr.json()
    except Exception as exc:
        log.warning("behavioral_judge_failed", error=str(exc))
        return result
    jchoices = jpayload.get("choices") or []
    if not jchoices:
        return result
    raw = (jchoices[0].get("message") or {}).get("content") or ""
    try:
        rubric_full = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("behavioral_judge_bad_json", snippet=raw[:200])
        return result
    result.rubric = {
        k: rubric_full.get(k) for k in
        ("task_understanding", "task_completion", "reasoning_efficiency",
         "action_compliance", "prompt_clarity")
    }
    ft = rubric_full.get("failure_tags") or []
    result.failure_tags = [str(t) for t in ft] if isinstance(ft, list) else []
    result.coach_notes = rubric_full.get("coach_notes")
    return result


async def run_comparison(
    *,
    pool,
    openrouter_api_key: str,
    judge_client: httpx.AsyncClient | None,
    judge_config: dict,
    models: list[str],
    canaries: tuple[BehavioralCanary, ...] = BEHAVIORAL_CANARIES,
) -> uuid.UUID:
    """Run every canary against every model, judge each, persist rows
    keyed by a shared comparison_id. Returns the comparison_id so the
    caller can immediately fetch the results.

    Sequential rather than concurrent — keeps OpenRouter rate-limit
    pressure low and makes the timing comparable (no head-of-line
    blocking between models).
    """
    comparison_id = uuid.uuid4()
    async with httpx.AsyncClient() as ext:
        for canary in canaries:
            for model in models:
                result = await _call_openrouter(
                    ext, openrouter_api_key, model, canary.prompt, canary.max_tokens,
                )
                result.canary_name = canary.name
                if result.error is None:
                    result = await _judge_canary(judge_client, judge_config, canary, result)
                await _persist_result(pool, comparison_id, result)
    return comparison_id


async def _persist_result(pool, comparison_id: uuid.UUID, r: CanaryRunResult) -> None:
    rubric_json = json.dumps(r.rubric) if r.rubric is not None else None
    tcalls_json = json.dumps([]) if r.response_text is not None else None  # no tools used
    tags = list(r.failure_tags or [])
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.behavioral_canary_runs
                (comparison_id, canary_name, prompt, target_provider, target_model,
                 response_text, tool_calls_made, prompt_tokens, completion_tokens,
                 duration_ms, status_code, error, rubric, failure_tags,
                 coach_notes, judge_cost_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12,
                    $13::jsonb, $14, $15, $16)
            """,
            comparison_id, r.canary_name, r.prompt, "openrouter", r.target_model,
            r.response_text, tcalls_json, r.prompt_tokens, r.completion_tokens,
            r.duration_ms, r.status_code, r.error, rubric_json, tags,
            r.coach_notes, r.judge_cost_usd,
        )


async def get_latest_comparison(pool) -> dict:
    """Return the most recent comparison_id's full result set, grouped
    by canary name with a row per model. Shape matches what the
    Behavior tab's comparison view expects.
    """
    async with pool.acquire() as conn:
        comp_row = await conn.fetchrow(
            "SELECT comparison_id, MAX(ts) AS ts FROM nautgate.behavioral_canary_runs "
            "GROUP BY comparison_id ORDER BY ts DESC LIMIT 1"
        )
        if comp_row is None:
            return {"comparison_id": None, "ts": None, "canaries": []}
        rows = await conn.fetch(
            """
            SELECT canary_name, target_model, response_text, prompt_tokens,
                   completion_tokens, duration_ms, status_code, error,
                   rubric, failure_tags, coach_notes
              FROM nautgate.behavioral_canary_runs
             WHERE comparison_id = $1
             ORDER BY canary_name, target_model
            """,
            comp_row["comparison_id"],
        )
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        if isinstance(d.get("rubric"), str):
            try: d["rubric"] = json.loads(d["rubric"])
            except (ValueError, TypeError): d["rubric"] = None
        grouped.setdefault(d["canary_name"], []).append(d)
    canaries_out = [
        {"name": name, "results": results}
        for name, results in grouped.items()
    ]
    return {
        "comparison_id": str(comp_row["comparison_id"]),
        "ts": comp_row["ts"].isoformat() if comp_row["ts"] else None,
        "canaries": canaries_out,
    }


async def quality_eval_config_or_default(pool) -> dict:
    """Wrapper that swallows config-fetch failures so an empty/broken
    quality_eval config doesn't crash the comparison endpoint.
    """
    try:
        return await quality_eval_config(pool)
    except Exception as exc:
        log.warning("behavioral_canary_judge_config_failed", error=str(exc))
        return {}
