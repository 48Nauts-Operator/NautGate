import pytest
from fastapi import HTTPException

from app.max_launches import bind_request, clear_launches, register_launch, validate_launch
from app.routes.v1 import _with_anthropic_subscription


@pytest.fixture(autouse=True)
def _clear():
    clear_launches()
    yield
    clear_launches()


def test_launch_capability_is_opaque_bound_and_expires():
    token, launch = register_launch(
        app="xnaut",
        project="NautGate",
        native_session="claude-session-1",
        run_id="run-1",
        owner_instance="local",
        ttl_seconds=60,
        now=100.0,
    )
    assert "NautGate" not in token
    assert validate_launch(token, now=159.0) == launch
    with pytest.raises(HTTPException) as exc:
        validate_launch(token, now=160.0)
    assert exc.value.status_code == 401


def test_unknown_launch_fails_closed():
    with pytest.raises(HTTPException) as exc:
        validate_launch("invented", now=1.0)
    assert exc.value.detail == "invalid_or_expired_max_launch"


def test_bound_request_carries_attribution_and_real_upstream_path():
    token, launch = register_launch(
        app="xnaut",
        project="NautGate",
        native_session="claude-session-1",
        run_id="run-1",
        owner_instance="local",
    )
    request = type("Request", (), {"state": type("State", (), {})()})()
    assert bind_request(request, token) == launch
    assert request.state.project_id == "NautGate"
    assert request.state.anthropic_upstream_path == "/v1/messages"


def test_bound_max_replaces_metered_key_without_mutating_cached_keys(monkeypatch):
    monkeypatch.setattr(
        "app.anthropic_subscription.subscription_token", lambda: "sk-ant-oat01-safe"
    )
    cached = {"anthropic": "sk-ant-api03-metered", "openai": "sk-openai"}
    selected = _with_anthropic_subscription(dict(cached))
    assert selected["anthropic"] == "sk-ant-oat01-safe"
    assert cached["anthropic"] == "sk-ant-api03-metered"
