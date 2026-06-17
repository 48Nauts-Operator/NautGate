"""LLM-Probing — pure helpers, transport selection, cycle + alert logic."""

from app import llm_probe
from app.drift_investigator import CanaryResult, TargetTransport, _select_transports


def test_models_match_loose():
    assert llm_probe._models_match("claude-opus-4-8", "claude-opus-4-8-20260514")
    assert llm_probe._models_match("openrouter/anthropic/claude-sonnet-4", "claude-sonnet-4")
    assert not llm_probe._models_match("claude-opus-4-8", "claude-haiku-4-5")
    assert llm_probe._models_match("anything", None)  # nothing to compare → not a mismatch


def test_response_sha_normalizes_whitespace_and_case():
    assert llm_probe._response_sha("  Hello   World ") == llm_probe._response_sha("hello world")
    assert llm_probe._response_sha("") is None
    assert llm_probe._response_sha(None) is None


def test_refusal_detection():
    assert llm_probe._is_refused("", ())
    assert llm_probe._is_refused("   ", ())
    assert llm_probe._is_refused("I cannot help with that request.", ())
    assert not llm_probe._is_refused("Sure — here is how a buffer overflow works...", ())


def test_chatgpt_oauth_transport_selected(monkeypatch):
    monkeypatch.setenv("NAUTGATE_CHATGPT_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("NAUTGATE_CHATGPT_ACCOUNT_ID", "acct")
    ts = _select_transports("openai", "gpt-5-codex", prefer_oauth=True)
    assert "chatgpt-oauth" in [t.via for t in ts]


def test_anthropic_oauth_transport_selected(monkeypatch):
    monkeypatch.setenv("NAUTGATE_ANTHROPIC_OAUTH_TOKEN", "tok")
    ts = _select_transports("anthropic", "claude-opus-4-8", prefer_oauth=True)
    assert "anthropic-oauth" in [t.via for t in ts]


async def test_cross_path_alerts(monkeypatch):
    inserted = []
    async def fake_alert(pool, **kw):
        inserted.append(kw)
    monkeypatch.setattr(llm_probe.queries, "insert_probe_alert", fake_alert)

    by_probe = {
        "quality_reason": {  # sub a full point worse → divergence
            "sub": {"tpb": 0.20, "qscore": 2.0, "refused": False, "sha": "a", "error": None},
            "metered": {"tpb": 0.20, "qscore": 5.0, "refused": False, "sha": "b", "error": None},
        },
        "tokenizer_fp": {  # >10% tokens/byte gap → divergence
            "sub": {"tpb": 0.30, "qscore": None, "refused": False, "sha": "c", "error": None},
            "metered": {"tpb": 0.20, "qscore": None, "refused": False, "sha": "d", "error": None},
        },
        "latency_ping": {  # identical → no alert
            "sub": {"tpb": 0.20, "qscore": None, "refused": False, "sha": "e", "error": None},
            "metered": {"tpb": 0.20, "qscore": None, "refused": False, "sha": "e", "error": None},
        },
    }
    await llm_probe._cross_path_alerts(None, "cid", "anthropic", "claude-x", by_probe)
    types = [a["alert_type"] for a in inserted]
    assert types.count("cross_path_divergence") == 2  # quality + tokenizer, not latency


def _fake_canary_result(canary, via, observed_model="claude-opus-4-8-20260514", error=None):
    return CanaryResult(
        canary_name=canary.name, via=via, target_provider="anthropic",
        target_model="claude-opus-4-8", prompt=canary.prompt,
        prompt_bytes=len(canary.prompt.encode("utf-8")), prompt_tokens=100,
        completion_tokens=5, response_text="OK", response_bytes=2, duration_ms=50,
        first_byte_ms=20, status_code=200, cost_usd=0.0, error=error,
        observed_model=observed_model,
    )


async def test_run_probe_cycle_persists_runs_and_flags_mismatch(monkeypatch):
    runs, alerts = [], []
    async def f_run(pool, **kw): runs.append(kw)
    async def f_alert(pool, **kw): alerts.append(kw)
    async def f_get_baseline(pool, **kw): return None
    async def f_upsert(pool, **kw): pass
    monkeypatch.setattr(llm_probe.queries, "insert_probe_run", f_run)
    monkeypatch.setattr(llm_probe.queries, "insert_probe_alert", f_alert)
    monkeypatch.setattr(llm_probe.queries, "get_probe_baseline", f_get_baseline)
    monkeypatch.setattr(llm_probe.queries, "upsert_probe_baseline", f_upsert)
    # Two legs: subscription (oauth) + metered (openrouter).
    monkeypatch.setattr(llm_probe, "_select_transports", lambda p, m, prefer_oauth: [
        TargetTransport(via="anthropic-oauth", base_url="x", api_key_env="K"),
        TargetTransport(via="openrouter", base_url="y", api_key_env="K2"),
    ])
    # Subscription leg returns a DIFFERENT model than requested → mismatch.
    async def fake_run_canary(client, canary, prov, model, transport, pricing):
        if transport.via == "anthropic-oauth":
            return _fake_canary_result(canary, "anthropic-oauth", observed_model="claude-haiku-4-5")
        return _fake_canary_result(canary, "openrouter")
    monkeypatch.setattr(llm_probe, "_run_canary", fake_run_canary)

    cid = await llm_probe.run_probe_cycle(
        pool=None, pricing=None, judge_client=None, judge_config={},
        targets=["anthropic/claude-opus-4-8"],
    )
    assert cid is not None
    # 2 legs × 5 probes
    assert len(runs) == 2 * len(llm_probe.PROBE_SUITE)
    assert any(a["alert_type"] == "model_mismatch" for a in alerts)


async def test_run_probe_cycle_no_transport_raises_auth_alert(monkeypatch):
    alerts = []
    async def f_alert(pool, **kw): alerts.append(kw)
    monkeypatch.setattr(llm_probe.queries, "insert_probe_alert", f_alert)
    monkeypatch.setattr(llm_probe, "_select_transports", lambda p, m, prefer_oauth: [])
    await llm_probe.run_probe_cycle(
        pool=None, pricing=None, judge_client=None, judge_config={},
        targets=["anthropic/claude-opus-4-8"],
    )
    assert any(a["alert_type"] == "auth_expired" for a in alerts)
