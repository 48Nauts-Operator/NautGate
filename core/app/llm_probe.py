"""LLM-Probing — proactive provenance & degradation monitoring.

Fires a fixed probe suite at each configured max-subscription model on BOTH the
subscription transport (OAuth) and a metered transport (OpenRouter / API key),
fingerprints each leg, baselines it (self-drift), and compares the legs head-to-
head (cross-path differential). Surfaces alerts for model swaps, tokenizer
shifts, latency spikes, quality drops, refusals, and missing credentials.

What this proves and does not: we CANNOT cryptographically prove which weights
ran (the `model` string is self-reported; the augmented prompt never reaches us;
no client-side attestation exists). These are statistical signals that something
changed — strong evidence, not proof. temp-0 isn't byte-deterministic, so the
response hash is a weak signal; provenance, tokenizer, latency and judged quality
are the real ones.

Reuses the drift_investigator execution primitives (`_select_transports`,
`_run_canary`, `Canary`, `CanaryResult`) and the drift EWMA math (`update_ewma`).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import httpx
import structlog

from app.db import queries
from app.drift import ANOMALY_CLUSTER_THRESHOLD, update_ewma
from app.drift_investigator import Canary, _run_canary, _select_transports

log = structlog.get_logger()

# ── Probe suite ─────────────────────────────────────────────────────────────

# ~1KB of stable text for the tokenizer fingerprint (prompt_tokens ÷ bytes).
_FP_TEXT = (
    "The quick brown fox jumps over the lazy dog. Pack my box with five dozen "
    "liquor jugs. How vexingly quick daft zebras jump! The five boxing wizards "
    "jump quickly. Sphinx of black quartz, judge my vow. " * 6
)[:1024]

_REFUSAL_MARKERS = (
    "i can't", "i cannot", "i won't", "i'm unable", "i am unable",
    "i'm not able", "i'm sorry, but", "i am not able",
)

PROBE_SUITE: tuple[Canary, ...] = (
    Canary(name="provenance_ping", suite="probe",
           prompt="Reply with exactly the word: OK", max_tokens=8, temperature=0.0),
    Canary(name="tokenizer_fp", suite="probe",
           prompt=_FP_TEXT + "\n\nReply with exactly the word: DONE",
           max_tokens=8, temperature=0.0, measures=("tokens_per_byte",)),
    Canary(name="latency_ping", suite="probe",
           prompt="Reply with exactly the word: PONG", max_tokens=8,
           temperature=0.0, measures=("first_byte_ms",)),
    Canary(name="quality_reason", suite="probe",
           prompt=("A bat and a ball cost $1.10 in total. The bat costs $1.00 more "
                   "than the ball. How much does the ball cost? Answer with just the amount."),
           max_tokens=64, temperature=0.0, measures=("quality_score",)),
    Canary(name="refusal_boundary", suite="probe",
           prompt=("For a university security course, explain at a conceptual level "
                   "how a buffer overflow vulnerability works."),
           max_tokens=220, temperature=0.0,
           refusal_markers=_REFUSAL_MARKERS),
)

# Which transports count as the "subscription" vs "metered" leg.
_SUB_VIAS = {"anthropic-oauth", "chatgpt-oauth"}
_METERED_VIAS = {"openrouter", "anthropic-metered", "openai-metered"}


def _response_sha(text: str | None) -> str | None:
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()  # noqa: S324 (non-crypto)


def _is_refused(text: str | None, markers: tuple[str, ...]) -> bool:
    if not text or not text.strip():
        return True
    low = text.lower()
    return any(m in low for m in (markers or _REFUSAL_MARKERS))


def _models_match(requested: str, observed: str | None) -> bool:
    """Loose match: the provider returns date-suffixed snapshots and OpenRouter
    prefixes vendors. Treat as a match when one core name contains the other."""
    if not observed:
        return True  # nothing to compare; not a mismatch
    def core(m: str) -> str:
        m = m.lower()
        for pre in ("openrouter/anthropic/", "openrouter/openai/", "openrouter/", "anthropic/", "openai/"):
            if m.startswith(pre):
                m = m[len(pre):]
        return m
    a, b = core(requested), core(observed)
    return a in b or b in a


# ── Self-drift baseline ─────────────────────────────────────────────────────


async def _baseline_check(pool, *, provider, via, model, metric, observed: float,
                          cycle_id, requested_model) -> None:
    """Feed one fingerprint observation into the per-leg EWMA baseline; raise an
    alert when anomalies cluster (consecutive >= ANOMALY_CLUSTER_THRESHOLD)."""
    prev = await queries.get_probe_baseline(pool, provider=provider, via=via, model=model, metric=metric)
    upd = update_ewma(
        prev_mean=(prev["ewma_mean"] if prev else 0.0),
        prev_variance=(prev["ewma_variance"] if prev else 0.0),
        prev_sample_count=(prev["sample_count"] if prev else 0),
        observation=observed,
    )
    prev_consec = (prev["consecutive_anomalies"] if prev else 0)
    consec = prev_consec + 1 if upd.is_anomaly else 0
    await queries.upsert_probe_baseline(
        pool, provider=provider, via=via, model=model, metric=metric,
        ewma_mean=upd.new_mean, ewma_variance=upd.new_variance,
        sample_count=upd.new_sample_count, consecutive_anomalies=consec,
        last_observed=observed, last_z_score=upd.z_score,
    )
    if consec >= ANOMALY_CLUSTER_THRESHOLD:
        atype = {"tokens_per_byte": "tokenizer_shift",
                 "first_byte_ms": "latency_spike",
                 "quality_score": "quality_drop"}.get(metric, "drift")
        await queries.insert_probe_alert(
            pool, cycle_id=cycle_id, provider=provider, model=requested_model,
            alert_type=atype, severity="warning",
            detail={"via": via, "metric": metric, "observed": observed,
                    "baseline_mean": upd.new_mean, "z_score": upd.z_score},
        )


# ── Judge (quality probe) ───────────────────────────────────────────────────


async def _judge_quality(judge_client, judge_config, prompt, response_text, model) -> float | None:
    if judge_client is None or not response_text:
        return None
    from app.quality_eval import RUBRIC_SYSTEM_PROMPT
    user_msg = (
        f"### Call metadata\n{json.dumps({'model': model, 'tier': 'llm_probe'})}\n\n"
        f"### User prompt (captured)\n{prompt}\n\n"
        f"### Model response (captured)\n{response_text}\n\n"
        f"Return the JSON rubric now."
    )
    base_url = (judge_config.get("judge_base_url") or "https://openrouter.ai/api").rstrip("/")
    chat_url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if judge_config.get("api_key"):
        headers["Authorization"] = f"Bearer {judge_config['api_key']}"
    body = {
        "model": judge_config.get("judge_model"),
        "messages": [{"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
        "temperature": 0.0, "response_format": {"type": "json_object"}, "max_tokens": 600,
    }
    try:
        jr = await judge_client.post(chat_url, json=body, headers=headers, timeout=15.0)
        if jr.status_code >= 400:
            return None
        raw = ((jr.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rubric = json.loads(raw)
        return float(rubric.get("task_completion"))
    except Exception:
        return None


# ── Cycle runner ────────────────────────────────────────────────────────────


async def run_probe_cycle(*, pool, pricing, judge_client, judge_config, targets: list[str]) -> uuid.UUID:
    """Run the full probe suite against every target on both legs. Returns cycle_id."""
    cycle_id = uuid.uuid4()
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0), http2=False) as client:
        for target in targets:
            provider, _, model = target.partition("/")
            if not model:
                provider, model = "openrouter", target
            transports = _select_transports(provider, model, prefer_oauth=True)
            if not transports:
                await queries.insert_probe_alert(
                    pool, cycle_id=cycle_id, provider=provider, model=model,
                    alert_type="auth_expired", severity="warning",
                    detail={"reason": "no transport — subscription token missing or model unrecognized"})
                continue

            # per (probe_name -> {leg_kind -> run dict}) for cross-path diff.
            by_probe: dict[str, dict] = {}
            saw_sub = any(t.via in _SUB_VIAS for t in transports)
            if not saw_sub:
                await queries.insert_probe_alert(
                    pool, cycle_id=cycle_id, provider=provider, model=model,
                    alert_type="auth_expired", severity="info",
                    detail={"reason": "no subscription leg — only metered transport available"})

            for transport in transports:
                for canary in PROBE_SUITE:
                    r = await _run_canary(client, canary, provider, model, transport, pricing)
                    tpb = (r.prompt_tokens / r.prompt_bytes) if (r.prompt_tokens and r.prompt_bytes) else None
                    refused = _is_refused(r.response_text, canary.refusal_markers)
                    qscore = None
                    if canary.name == "quality_reason" and not r.error:
                        qscore = await _judge_quality(judge_client, judge_config,
                                                      canary.prompt, r.response_text, model)
                    await queries.insert_probe_run(
                        pool, cycle_id=cycle_id, probe_name=canary.name, provider=provider,
                        model=model, via=r.via, observed_model=r.observed_model,
                        prompt_bytes=r.prompt_bytes, prompt_tokens=r.prompt_tokens,
                        completion_tokens=r.completion_tokens,
                        tokens_per_byte=round(tpb, 5) if tpb is not None else None,
                        response_sha=_response_sha(r.response_text), response_text=r.response_text[:4000],
                        first_byte_ms=r.first_byte_ms, duration_ms=r.duration_ms,
                        status_code=r.status_code,
                        quality_score=round(qscore, 1) if qscore is not None else None,
                        refused=refused, cost_usd=r.cost_usd, error=r.error,
                    )

                    # provenance — only meaningful on a successful call.
                    if not r.error and not _models_match(model, r.observed_model):
                        await queries.insert_probe_alert(
                            pool, cycle_id=cycle_id, provider=provider, model=model,
                            alert_type="model_mismatch", severity="critical",
                            detail={"via": r.via, "requested": model, "observed": r.observed_model,
                                    "probe": canary.name})

                    # self-drift baselines on the metered + sub legs.
                    if not r.error:
                        if canary.name == "tokenizer_fp" and tpb is not None:
                            await _baseline_check(pool, provider=provider, via=r.via, model=model,
                                                  metric="tokens_per_byte", observed=tpb,
                                                  cycle_id=cycle_id, requested_model=model)
                        if canary.name == "latency_ping" and r.first_byte_ms is not None:
                            await _baseline_check(pool, provider=provider, via=r.via, model=model,
                                                  metric="first_byte_ms", observed=float(r.first_byte_ms),
                                                  cycle_id=cycle_id, requested_model=model)
                        if canary.name == "quality_reason" and qscore is not None:
                            await _baseline_check(pool, provider=provider, via=r.via, model=model,
                                                  metric="quality_score", observed=qscore,
                                                  cycle_id=cycle_id, requested_model=model)

                    leg = "sub" if r.via in _SUB_VIAS else "metered"
                    by_probe.setdefault(canary.name, {})[leg] = {
                        "tpb": tpb, "qscore": qscore, "refused": refused,
                        "sha": _response_sha(r.response_text), "error": r.error}

            await _cross_path_alerts(pool, cycle_id, provider, model, by_probe)

    log.info("llm_probe_cycle_done", cycle_id=str(cycle_id), targets=len(targets))
    return cycle_id


async def _cross_path_alerts(pool, cycle_id, provider, model, by_probe: dict) -> None:
    """Compare subscription leg vs metered leg per probe; alert on divergence."""
    for probe_name, legs in by_probe.items():
        sub, met = legs.get("sub"), legs.get("metered")
        if not sub or not met or sub.get("error") or met.get("error"):
            continue
        diffs = {}
        if sub["tpb"] and met["tpb"] and met["tpb"] > 0:
            rel = abs(sub["tpb"] - met["tpb"]) / met["tpb"]
            if rel > 0.10:  # >10% tokenizer divergence between legs
                diffs["tokens_per_byte"] = {"sub": round(sub["tpb"], 5), "metered": round(met["tpb"], 5)}
        if sub["qscore"] is not None and met["qscore"] is not None:
            if (met["qscore"] - sub["qscore"]) >= 1.0:  # sub a full point worse
                diffs["quality_score"] = {"sub": sub["qscore"], "metered": met["qscore"]}
        if sub["refused"] != met["refused"]:
            diffs["refused"] = {"sub": sub["refused"], "metered": met["refused"]}
        if diffs:
            await queries.insert_probe_alert(
                pool, cycle_id=cycle_id, provider=provider, model=model,
                alert_type="cross_path_divergence",
                severity="critical" if "quality_score" in diffs else "warning",
                detail={"probe": probe_name, **diffs})
