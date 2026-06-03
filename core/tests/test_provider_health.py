"""Day 5c — provider_health streak + demote-on-3-empties + recovery."""

import pytest

from app.provider_health import UNHEALTHY_THRESHOLD, ProviderHealthTracker
from app.scoring import resolve_healthy

# --- ProviderHealthTracker ------------------------------------------------


def test_initial_state_is_healthy():
    t = ProviderHealthTracker()
    assert t.is_unhealthy("openai", "gpt-4o-mini") is False


def test_three_in_a_row_marks_unhealthy():
    t = ProviderHealthTracker()
    for _ in range(UNHEALTHY_THRESHOLD):
        t.record("openai", "gpt-4o", was_empty=True)
    assert t.is_unhealthy("openai", "gpt-4o") is True


def test_two_in_a_row_does_not_mark_unhealthy():
    t = ProviderHealthTracker()
    t.record("openai", "gpt-4o", was_empty=True)
    t.record("openai", "gpt-4o", was_empty=True)
    assert t.is_unhealthy("openai", "gpt-4o") is False


def test_non_empty_resets_streak():
    t = ProviderHealthTracker()
    t.record("openai", "gpt-4o", was_empty=True)
    t.record("openai", "gpt-4o", was_empty=True)
    t.record("openai", "gpt-4o", was_empty=False)
    # Now feed two more empties — should NOT trip threshold.
    t.record("openai", "gpt-4o", was_empty=True)
    t.record("openai", "gpt-4o", was_empty=True)
    assert t.is_unhealthy("openai", "gpt-4o") is False


def test_recovery_clears_unhealthy_state():
    t = ProviderHealthTracker()
    for _ in range(UNHEALTHY_THRESHOLD):
        t.record("openai", "gpt-4o", was_empty=True)
    assert t.is_unhealthy("openai", "gpt-4o") is True
    t.record("openai", "gpt-4o", was_empty=False)
    assert t.is_unhealthy("openai", "gpt-4o") is False


def test_per_pair_isolation():
    t = ProviderHealthTracker()
    for _ in range(UNHEALTHY_THRESHOLD):
        t.record("openai", "gpt-4o-mini", was_empty=True)
    assert t.is_unhealthy("openai", "gpt-4o-mini") is True
    # A different model on the same provider stays healthy.
    assert t.is_unhealthy("openai", "gpt-4o") is False
    # A different provider stays healthy.
    assert t.is_unhealthy("anthropic", "claude-haiku-4-5") is False


def test_custom_threshold():
    t = ProviderHealthTracker(threshold=2)
    t.record("p", "m", was_empty=True)
    assert t.is_unhealthy("p", "m") is False
    t.record("p", "m", was_empty=True)
    assert t.is_unhealthy("p", "m") is True


# --- resolve_healthy --------------------------------------------------------


@pytest.fixture
def table():
    return {
        "fast": {
            "primary": {"provider": "openai", "model": "gpt-4o-mini"},
            "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        },
        "balanced": {
            "primary": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "fallback": {"provider": "openai", "model": "gpt-4o"},
        },
        "deep": {  # deliberately no fallback
            "primary": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
    }


def test_resolve_healthy_returns_primary_when_all_healthy(table):
    r = resolve_healthy("fast", table, lambda *_: False, enforce_subscription_ban=False)
    assert r.provider == "openai"
    assert r.model == "gpt-4o-mini"


def test_resolve_healthy_falls_back_when_primary_unhealthy(table):
    def is_unhealthy(provider, model):
        return (provider, model) == ("openai", "gpt-4o-mini")

    r = resolve_healthy("fast", table, is_unhealthy, enforce_subscription_ban=False)
    assert r.provider == "anthropic"
    assert r.model == "claude-haiku-4-5"


def test_resolve_healthy_returns_primary_when_no_fallback_even_if_unhealthy(table):
    """If unhealthy primary has no fallback, we'd rather use it than strand the request."""
    r = resolve_healthy("deep", table, lambda *_: True, enforce_subscription_ban=False)
    assert r.provider == "anthropic"
    assert r.model == "claude-sonnet-4-6"


def test_resolve_healthy_respects_specific_pair_only(table):
    """Marking only one specific (provider, model) unhealthy doesn't blanket-skip its provider."""

    # balanced primary is anthropic/claude-haiku-4-5; mark openai/gpt-4o-mini unhealthy.
    def is_unhealthy(provider, model):
        return (provider, model) == ("openai", "gpt-4o-mini")

    r = resolve_healthy("balanced", table, is_unhealthy, enforce_subscription_ban=False)
    assert r.provider == "anthropic"  # primary used as-is
