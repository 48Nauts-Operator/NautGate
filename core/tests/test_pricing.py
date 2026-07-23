"""Pricing config + per-call cost computation."""

from pathlib import Path

import pytest

from app.pricing import ModelPrice, PricingTable


@pytest.fixture
def pricing(tmp_path):
    cfg = tmp_path / "pricing.yaml"
    cfg.write_text(
        """
pricing:
  anthropic/claude-haiku-4.5:
    input: 1.0
    output: 5.0
  openai/gpt-4o-mini:
    input: 0.15
    output: 0.6
""",
        encoding="utf-8",
    )
    return PricingTable.from_yaml(cfg)


def test_lookup_known_model(pricing):
    p = pricing.lookup("anthropic", "claude-haiku-4.5")
    assert isinstance(p, ModelPrice)
    assert p.input == 1.0 and p.output == 5.0


def test_lookup_unknown_returns_none(pricing):
    assert pricing.lookup("unknown", "model") is None
    assert pricing.lookup("anthropic", "missing") is None
    assert pricing.lookup(None, "x") is None
    assert pricing.lookup("x", None) is None


def test_compute_cost_basic(pricing):
    # 1k prompt @ $1/M = $0.001; 500 completion @ $5/M = $0.0025; total $0.0035
    cost = pricing.compute_cost(
        "anthropic", "claude-haiku-4.5", prompt_tokens=1000, completion_tokens=500
    )
    assert cost == pytest.approx(0.0035, rel=1e-6)


def test_compute_cost_unknown_model_returns_none(pricing):
    assert pricing.compute_cost("ollama", "qwen3", prompt_tokens=1, completion_tokens=1) is None


def test_compute_cost_no_usage_returns_none(pricing):
    assert (
        pricing.compute_cost(
            "anthropic", "claude-haiku-4.5", prompt_tokens=None, completion_tokens=None
        )
        is None
    )


def test_compute_cost_one_side_zero(pricing):
    # Prompt-only cost (no completion yet, e.g., partial stream): still computes.
    cost = pricing.compute_cost(
        "anthropic", "claude-haiku-4.5", prompt_tokens=1000, completion_tokens=None
    )
    assert cost == pytest.approx(0.001, rel=1e-6)


@pytest.fixture
def cache_pricing(tmp_path):
    cfg = tmp_path / "pricing.yaml"
    cfg.write_text(
        """
pricing:
  anthropic/claude-opus-4:
    input: 15.0
    output: 75.0
    cache_read: 1.5
    cache_write: 18.75
  openai/gpt-4o-mini:
    input: 0.15
    output: 0.6
""",
        encoding="utf-8",
    )
    return PricingTable.from_yaml(cfg)


def test_compute_cost_cache_tiers(cache_pricing):
    # fresh 245 @ $15/M = 0.003675
    # cache_read 18420 @ $1.5/M = 0.02763
    # cache_write 1000 @ $18.75/M = 0.01875
    # completion 512 @ $75/M = 0.0384
    cost = cache_pricing.compute_cost(
        "anthropic",
        "claude-opus-4",
        prompt_tokens=245,
        completion_tokens=512,
        cache_read_tokens=18420,
        cache_write_tokens=1000,
    )
    assert cost == pytest.approx(0.003675 + 0.02763 + 0.01875 + 0.0384, rel=1e-6)


def test_compute_cost_cache_read_only(cache_pricing):
    cost = cache_pricing.compute_cost(
        "anthropic",
        "claude-opus-4",
        prompt_tokens=None,
        completion_tokens=None,
        cache_read_tokens=10000,
    )
    assert cost == pytest.approx(10000 * 1.5 / 1_000_000, rel=1e-6)


def test_compute_cost_unpriced_tier_falls_back_to_input(cache_pricing):
    # gpt-4o-mini has no cache_read/cache_write → both fall back to input rate.
    cost = cache_pricing.compute_cost(
        "openai",
        "gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_tokens=1000,
        cache_write_tokens=1000,
    )
    assert cost == pytest.approx(2000 * 0.15 / 1_000_000, rel=1e-6)


def test_compute_cost_backward_compatible_two_arg(cache_pricing):
    # Old call sites that don't pass cache kwargs still work.
    cost = cache_pricing.compute_cost(
        "anthropic", "claude-opus-4", prompt_tokens=1000, completion_tokens=0
    )
    assert cost == pytest.approx(1000 * 15.0 / 1_000_000, rel=1e-6)


def test_missing_yaml_loads_empty():
    p = PricingTable.from_yaml(Path("/nonexistent/pricing.yaml"))
    assert p.size == 0
    assert p.compute_cost("anthropic", "x", prompt_tokens=1, completion_tokens=1) is None


def test_invalid_yaml_loads_empty(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("pricing:\n  - this is :: bad", encoding="utf-8")
    p = PricingTable.from_yaml(cfg)
    assert p.size == 0


def test_warn_once_for_missing(pricing, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    pricing.compute_cost("ollama", "qwen3", prompt_tokens=1, completion_tokens=1)
    pricing.compute_cost("ollama", "qwen3", prompt_tokens=2, completion_tokens=2)
    warnings = [r for r in caplog.records if "pricing_unknown" in r.getMessage()]
    assert len(warnings) == 1


def test_repo_pricing_loads():
    """The committed pricing.yaml parses without errors."""
    repo_cfg = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
    p = PricingTable.from_yaml(repo_cfg)
    assert p.size > 0
    assert p.lookup("anthropic", "claude-haiku-4.5") is not None
    # Snapshot IDs that Claude Code actually sends must resolve to a price.
    for snap in (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ):
        assert p.lookup("anthropic", snap) is not None, f"missing pricing for {snap}"


def test_resolve_pricing_provider_passthrough_anthropic():
    from app.routes.v1 import _resolve_pricing_provider as resolve

    # Pure passthrough → derive from model prefix
    assert resolve("passthrough", None, "claude-opus-4-7") == "anthropic"
    assert resolve("passthrough", None, "gpt-4o-mini") == "openai"
    assert resolve("passthrough", None, "gemini-2.5-flash") == "gemini"
    # actual_provider wins over heuristic
    assert resolve("passthrough", "anthropic", "claude-opus-4-7") == "anthropic"
    # Already-real provider is left alone
    assert resolve("openrouter", None, "any") == "openrouter"
    # chatgpt-oauth Codex maps to openai
    assert resolve("chatgpt-oauth", None, "gpt-5.4") == "openai"
    # Unknown family with no actual_provider → returns the passthrough sentinel
    assert resolve("passthrough", None, "qwen-7b") == "passthrough"
    assert resolve("passthrough", None, None) == "passthrough"
