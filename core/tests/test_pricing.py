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
