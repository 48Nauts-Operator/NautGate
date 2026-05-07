"""Week 2 — plugin contract tests.

Real PluginRegistry against an httpx.MockTransport that stands in for an
extension service. Covers config loading, no-op behavior, before_route
aggregation, fire-and-forget hooks, timeout fall-through.
"""

import asyncio
import json
import uuid

import httpx
import pytest

from app.plugins import (
    BEFORE_ROUTE_DEFAULT_TIMEOUT_MS,
    Extension,
    PluginRegistry,
    _jsonable,
)

# --- _jsonable ----------------------------------------------------------


def test_jsonable_coerces_uuid_and_nested():
    out = _jsonable(
        {
            "id": uuid.uuid4(),
            "nested": {"id": uuid.uuid4()},
            "list": [uuid.uuid4()],
        }
    )
    s = json.dumps(out)  # would raise if any UUID leaked
    assert s


# --- from_config -----------------------------------------------------------


def test_from_config_missing_file_returns_empty():
    reg = PluginRegistry.from_config("/nonexistent/path/to/nautgate.yaml")
    assert reg.is_empty


def test_from_config_none_returns_empty():
    reg = PluginRegistry.from_config(None)
    assert reg.is_empty


def test_from_config_loads_valid_yaml(tmp_path):
    cfg = tmp_path / "nautgate.yaml"
    cfg.write_text(
        """
extensions:
  sb-capture:
    base_url: http://sb-capture:8001
    hooks: [on_request, on_response, on_outcome]
    timeout_ms: 100
  sb-brain:
    base_url: http://sb-brain:8002/
    hooks: [before_route, on_outcome]
    timeout_ms_before_route: 30
""",
        encoding="utf-8",
    )
    reg = PluginRegistry.from_config(cfg)
    assert not reg.is_empty
    names = {e.name for e in reg.extensions}
    assert names == {"sb-capture", "sb-brain"}
    capture = next(e for e in reg.extensions if e.name == "sb-capture")
    assert capture.base_url == "http://sb-capture:8001"  # trailing slash stripped
    assert "on_request" in capture.hooks
    assert capture.timeout_ms == 100
    brain = next(e for e in reg.extensions if e.name == "sb-brain")
    assert brain.base_url == "http://sb-brain:8002"
    assert brain.timeout_ms_before_route == 30


def test_from_config_skips_invalid_entries(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        """
extensions:
  no_url: {hooks: [on_request]}
  malformed: "not a dict"
  good:
    base_url: http://x
    hooks: [on_request, FAKE_HOOK]
""",
        encoding="utf-8",
    )
    reg = PluginRegistry.from_config(cfg)
    assert {e.name for e in reg.extensions} == {"good"}
    good = reg.extensions[0]
    # FAKE_HOOK gets filtered out, on_request stays.
    assert good.hooks == ("on_request",)


def test_from_config_handles_invalid_yaml(tmp_path):
    cfg = tmp_path / "broken.yaml"
    cfg.write_text(":\n  - this is :: not :: valid", encoding="utf-8")
    reg = PluginRegistry.from_config(cfg)
    assert reg.is_empty


# --- Empty registry is a no-op --------------------------------------------


@pytest.mark.asyncio
async def test_empty_registry_before_route_returns_default():
    reg = PluginRegistry([])
    out = await reg.call_before_route({})
    assert out == {"brain_hints": {}, "banned_models": [], "preferred_tier": None}
    await reg.aclose()


@pytest.mark.asyncio
async def test_empty_registry_dispatch_is_safe():
    reg = PluginRegistry([])
    reg.dispatch_on_request({"x": 1})
    reg.dispatch_on_response({"x": 1})
    reg.dispatch_after_route({"x": 1})
    reg.dispatch_on_outcome({"x": 1})
    await reg.aclose()


# --- Real registry against MockTransport ---------------------------------


def _registry_with_mock(handler):
    """Build a registry whose httpx client uses MockTransport."""
    reg = PluginRegistry(
        [
            Extension(
                name="ext1",
                base_url="http://ext1.local",
                hooks=("before_route", "on_request", "on_response", "after_route", "on_outcome"),
                timeout_ms=200,
                timeout_ms_before_route=50,
            )
        ]
    )

    async def aclose_orig():
        pass

    transport = httpx.MockTransport(handler)
    reg._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.0))
    return reg


@pytest.mark.asyncio
async def test_before_route_aggregates_extension_response():
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/before_route":
            return httpx.Response(
                200,
                json={
                    "brain_hints": {"reason": "looks like a code question"},
                    "banned_models": ["gpt-4o-mini"],
                    "preferred_tier": "deep",
                },
            )
        return httpx.Response(200, json={})

    reg = _registry_with_mock(handler)
    out = await reg.call_before_route({"agent_id": "alice"})
    await reg.aclose()
    assert seen_paths == ["/v1/before_route"]
    assert out["preferred_tier"] == "deep"
    assert "gpt-4o-mini" in out["banned_models"]
    assert out["brain_hints"]["reason"] == "looks like a code question"


@pytest.mark.asyncio
async def test_before_route_swallows_extension_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    reg = _registry_with_mock(handler)
    out = await reg.call_before_route({})
    await reg.aclose()
    # Default empty result; no exception.
    assert out == {"brain_hints": {}, "banned_models": [], "preferred_tier": None}


@pytest.mark.asyncio
async def test_before_route_timeout_falls_through():
    async def slow_handler(request: httpx.Request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={})

    reg = _registry_with_mock(slow_handler)
    # Override the per-ext timeout to a tight value.
    reg.extensions[0] = Extension(
        name="ext1",
        base_url="http://ext1.local",
        hooks=("before_route",),
        timeout_ms_before_route=20,
    )
    out = await reg.call_before_route({})
    await reg.aclose()
    assert out["preferred_tier"] is None  # timeout → empty result


@pytest.mark.asyncio
async def test_dispatch_on_request_fires_post():
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/on_request":
            received.append(json.loads(request.content))
        return httpx.Response(200, json={})

    reg = _registry_with_mock(handler)
    reg.dispatch_on_request({"decision_id": uuid.uuid4(), "agent_id": "alice"})
    # Wait for the fire-and-forget task to complete.
    await asyncio.sleep(0.05)
    await reg.aclose()
    assert len(received) == 1
    assert received[0]["agent_id"] == "alice"
    assert isinstance(received[0]["decision_id"], str)  # UUID coerced


@pytest.mark.asyncio
async def test_fire_and_forget_swallows_extension_failure():
    """An extension that 500s during on_outcome must not break the gateway."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    reg = _registry_with_mock(handler)
    reg.dispatch_on_outcome({"decision_id": uuid.uuid4()})
    # Give the task a moment to run; should NOT raise.
    await asyncio.sleep(0.05)
    await reg.aclose()


@pytest.mark.asyncio
async def test_only_subscribed_hooks_fire():
    """An extension that doesn't subscribe to a hook is never called for it."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={})

    # ext1 only subscribes to on_request.
    reg = PluginRegistry(
        [
            Extension(
                name="picky",
                base_url="http://picky.local",
                hooks=("on_request",),
            )
        ]
    )
    reg._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reg.dispatch_on_request({"x": 1})
    reg.dispatch_on_response({"x": 1})
    reg.dispatch_on_outcome({"x": 1})
    await asyncio.sleep(0.05)
    await reg.aclose()
    # Only on_request fired.
    assert seen_paths == ["/v1/on_request"]


# --- Defaults ---------------------------------------------------------------


def test_default_before_route_timeout_is_50ms():
    assert BEFORE_ROUTE_DEFAULT_TIMEOUT_MS == 50
