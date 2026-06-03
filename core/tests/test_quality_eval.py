"""Quality eval hook: triggers, sampling, judge call, persistence."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import quality_eval


# ── should_evaluate logic ──────────────────────────────────────────────────


def _make_pair(**overrides) -> tuple[dict, dict]:
    decision = {
        "decision_model": "openrouter/openrouter/deepseek/deepseek-chat",
        "decision_provider": "openrouter",
        "classified_tier": "balanced",
        "classified_score": 5.0,
        "classified_sensitivity": "low",
        "prompt_body": "Refactor this please.",
        "prompt_excerpt": "Refactor this please.",
        "tools_count": 0,
        "bloat_score": None,
    }
    outcome = {
        "status_code": 200,
        "was_empty": False,
        "was_truncated": False,
        "client_disconnected": False,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "reasoning_tokens": 0,
        "duration_ms": 1200,
        "response_body": '{"choices":[{"message":{"content":"Done."}}]}',
        "tool_calls_made": None,
    }
    dec_over = overrides.get("decision", {})
    out_over = overrides.get("outcome", {})
    decision.update(dec_over)
    outcome.update(out_over)
    return decision, outcome


def _config(**overrides) -> dict:
    base = {"enabled": True, "sample_rate": 0.0, "daily_cost_cap_usd": 5.0}
    base.update(overrides)
    return base


def test_should_evaluate_disabled_short_circuits():
    decision, outcome = _make_pair()
    assert quality_eval.should_evaluate(decision, outcome, _config(enabled=False)) \
        == (False, "disabled")


def test_should_evaluate_sensitive_blocks():
    decision, outcome = _make_pair(decision={"classified_sensitivity": "secret"})
    assert quality_eval.should_evaluate(decision, outcome, _config(sample_rate=1.0)) \
        == (False, "sensitive")


def test_should_evaluate_was_empty_anomaly():
    decision, outcome = _make_pair(outcome={"was_empty": True})
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config())
    assert ok is True
    assert reason == "anomaly:was_empty"


def test_should_evaluate_status_error_anomaly():
    decision, outcome = _make_pair(outcome={"status_code": 502})
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config())
    assert ok is True
    assert reason == "anomaly:status_502"


def test_should_evaluate_disconnect_anomaly():
    decision, outcome = _make_pair(outcome={"client_disconnected": True})
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config())
    assert (ok, reason) == (True, "anomaly:client_disconnected")


def test_should_evaluate_bloat_anomaly():
    decision, outcome = _make_pair(decision={"bloat_score": 0.85})
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config())
    assert (ok, reason) == (True, "anomaly:bloat")


def test_should_evaluate_no_tool_calls_anomaly():
    decision, outcome = _make_pair(
        decision={"tools_count": 3},
        outcome={"tool_calls_made": None},
    )
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config())
    assert (ok, reason) == (True, "anomaly:no_tool_calls")


def test_should_evaluate_sampling_rate_zero_never_samples():
    decision, outcome = _make_pair()
    cfg = _config(sample_rate=0.0)
    # 100 calls; never picked.
    for _ in range(100):
        ok, _reason = quality_eval.should_evaluate(decision, outcome, cfg)
        assert ok is False


def test_should_evaluate_sampling_rate_one_always_samples():
    decision, outcome = _make_pair()
    cfg = _config(sample_rate=1.0)
    ok, reason = quality_eval.should_evaluate(decision, outcome, cfg)
    assert (ok, reason) == (True, "sample")


def test_should_evaluate_sampling_uses_random(monkeypatch):
    decision, outcome = _make_pair()
    monkeypatch.setattr(quality_eval.random, "random", lambda: 0.05)
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config(sample_rate=0.10))
    assert (ok, reason) == (True, "sample")
    monkeypatch.setattr(quality_eval.random, "random", lambda: 0.50)
    ok, reason = quality_eval.should_evaluate(decision, outcome, _config(sample_rate=0.10))
    assert ok is False


# ── _strip_fences / _make_user_message ─────────────────────────────────────


def test_strip_fences_handles_fenced_json():
    assert quality_eval._strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert quality_eval._strip_fences("```\n{\"a\":1}\n```") == '{"a":1}'
    assert quality_eval._strip_fences('{"a":1}') == '{"a":1}'


def test_make_user_message_includes_meta_and_bodies():
    decision, outcome = _make_pair()
    msg = quality_eval._make_user_message(decision, outcome)
    assert "Call metadata" in msg
    assert "Refactor this please" in msg
    assert "Done." in msg
    assert '"completion_tokens": 50' in msg


# ── _call_judge with mocked httpx ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_judge_happy_path():
    decision, outcome = _make_pair()
    cfg = _config(judge_provider="openai", judge_model="gpt-4o-mini",
                  judge_base_url="https://api.openai.com")
    cfg["api_key"] = "sk-test"

    rubric_json = {
        "task_understanding": 4, "task_completion": 4,
        "reasoning_efficiency": 3, "prompt_clarity": 3,
        "failure_tags": [], "suggested_prompt": "",
        "coach_notes": "Looks fine.",
    }
    judge_response = MagicMock()
    judge_response.status_code = 200
    judge_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps(rubric_json)}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 80},
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=judge_response)

    rubric, telem = await quality_eval._call_judge(client, cfg, decision, outcome)
    assert rubric == rubric_json
    assert telem["prompt_tokens"] == 1200
    assert telem["completion_tokens"] == 80
    assert telem["judge_model"] == "gpt-4o-mini"
    assert telem["judge_latency_ms"] is not None
    client.post.assert_awaited_once()
    # Verify the Bearer header was sent
    sent_headers = client.post.await_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_call_judge_http_error_returns_none():
    decision, outcome = _make_pair()
    cfg = _config(judge_provider="openai", judge_model="gpt-4o-mini")
    cfg["api_key"] = "sk-test"
    bad = MagicMock()
    bad.status_code = 429
    bad.text = "rate limited"
    client = MagicMock()
    client.post = AsyncMock(return_value=bad)
    rubric, telem = await quality_eval._call_judge(client, cfg, decision, outcome)
    assert rubric is None
    assert telem["judge_latency_ms"] is not None  # still populated


@pytest.mark.asyncio
async def test_call_judge_timeout_returns_none():
    import httpx
    decision, outcome = _make_pair()
    cfg = _config(judge_provider="openai", judge_model="gpt-4o-mini")
    cfg["api_key"] = "sk-test"
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    rubric, _telem = await quality_eval._call_judge(client, cfg, decision, outcome)
    assert rubric is None


@pytest.mark.asyncio
async def test_call_judge_bad_json_returns_none():
    decision, outcome = _make_pair()
    cfg = _config(judge_provider="openai", judge_model="gpt-4o-mini")
    cfg["api_key"] = "sk-test"
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    rubric, _ = await quality_eval._call_judge(client, cfg, decision, outcome)
    assert rubric is None


# ── process_quality short-circuits ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_quality_returns_silently_with_no_pool():
    await quality_eval.process_quality(None, decision_id="abc", judge_client=None)


@pytest.mark.asyncio
async def test_process_quality_returns_silently_with_no_judge_client():
    fake_pool = MagicMock()
    await quality_eval.process_quality(fake_pool, decision_id="abc", judge_client=None)


@pytest.mark.asyncio
async def test_process_quality_skips_when_disabled(monkeypatch):
    fake_pool = MagicMock()
    fake_client = MagicMock()
    async def _cfg(_pool): return {"enabled": False}
    monkeypatch.setattr(quality_eval, "_get_config", _cfg)
    # If it tried to call the judge, this would explode.
    await quality_eval.process_quality(fake_pool, decision_id="abc",
                                        judge_client=fake_client)
