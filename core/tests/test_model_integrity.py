import httpx
import pytest

from app.audit_receipt import environment_evidence, sampling_evidence
from app.model_integrity import WeightsResolver


def test_sampling_evidence_extracts_knobs_from_the_request_payload():
    payload = {"model": "qwen2:72b", "temperature": 0.7, "seed": 1, "messages": []}
    assert sampling_evidence(payload) == {
        "sampling_temperature": 0.7,
        "sampling_top_p": None,
        "sampling_seed": 1,
    }


def test_environment_evidence_reads_harness_and_declared_sandbox_headers():
    headers = {"user-agent": "claude-cli/2.1.0 (external, cli)", "x-nautgate-sandbox-id": "gitvm-7"}
    assert environment_evidence(headers, capture_path="gateway") == {
        "env_harness": "claude-cli/2.1.0 (external, cli)",
        "env_sandbox_id": "gitvm-7",
        "env_capture_path": "gateway",
    }


def test_environment_evidence_without_headers_stays_null():
    assert environment_evidence({}, capture_path="ingest") == {
        "env_harness": None,
        "env_sandbox_id": None,
        "env_capture_path": "ingest",
    }


@pytest.mark.asyncio
async def test_resolver_pins_ollama_weights_by_manifest_digest():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2:72b", "digest": "d3adb33f" * 8}]},
        )

    resolver = WeightsResolver(transport=httpx.MockTransport(handler))
    evidence = await resolver.resolve("ollama", "qwen2:72b")
    assert evidence["weights_digest"] == "sha256:" + "d3adb33f" * 8
    assert evidence["weights_digest_source"] == "ollama-manifest"
    assert evidence["weights_resolved_at"]


@pytest.mark.asyncio
async def test_resolver_fails_open_and_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    resolver = WeightsResolver(transport=httpx.MockTransport(handler))
    assert await resolver.resolve("ollama", "qwen2:72b") == {}


@pytest.mark.asyncio
async def test_resolver_ignores_providers_without_inspectable_weights():
    resolver = WeightsResolver(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert await resolver.resolve("anthropic", "claude-opus-5") == {}


@pytest.mark.asyncio
async def test_resolver_caches_the_digest_per_model():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"models": [{"name": "m", "digest": "aa" * 32}]})

    resolver = WeightsResolver(transport=httpx.MockTransport(handler))
    await resolver.resolve("ollama", "m")
    await resolver.resolve("ollama", "m")
    assert calls["n"] == 1
