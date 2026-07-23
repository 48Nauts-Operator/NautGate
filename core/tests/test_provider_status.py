"""Provider status — passive bucketing, retry-after parsing, heartbeat classify."""

from app import provider_heartbeat
from app.anthropic_oauth_forwarder import _parse_retry_after
from app.db import queries
from app.drift_investigator import CanaryResult, TargetTransport


def test_parse_retry_after():
    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("9999") == 4.0  # capped at _RETRY_CAP_S
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("Wed, 21 Oct 2025 07:28:00 GMT") is None  # HTTP-date ignored


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return self._rows


async def test_provider_status_buckets():
    rows = [
        {
            "provider": "openrouter",
            "total": 50,
            "ok": 50,
            "overloaded": 0,
            "rate_limited": 0,
            "errors": 0,
            "retries_absorbed": 0,
            "last_seen": None,
        },
        {
            "provider": "anthropic-oauth",
            "total": 100,
            "ok": 73,
            "overloaded": 27,
            "rate_limited": 0,
            "errors": 0,
            "retries_absorbed": 5,
            "last_seen": None,
        },
        {
            "provider": "down-one",
            "total": 10,
            "ok": 0,
            "overloaded": 10,
            "rate_limited": 0,
            "errors": 0,
            "retries_absorbed": 0,
            "last_seen": None,
        },
    ]
    out = await queries.get_provider_status(_FakePool(rows), minutes=10)
    assert out["openrouter"]["status"] == "up"
    a = out["anthropic-oauth"]
    assert a["status"] == "degraded"
    # overload events fold in absorbed retries: (27 + 5) / (73 + 27 + 5) = 0.3048
    assert abs(a["overload_pct"] - 0.3048) < 0.001
    assert a["retries_absorbed"] == 5
    assert out["down-one"]["status"] == "down"


async def test_provider_status_empty():
    out = await queries.get_provider_status(_FakePool([]), minutes=10)
    assert out == {}


def _cr(status_code, error=None):
    return CanaryResult(
        canary_name="heartbeat",
        via="openrouter",
        target_provider="openrouter",
        target_model="m",
        prompt="p",
        prompt_bytes=1,
        prompt_tokens=1,
        completion_tokens=1,
        response_text="OK",
        response_bytes=2,
        duration_ms=40,
        first_byte_ms=20,
        status_code=status_code,
        cost_usd=0.0,
        error=error,
    )


async def test_heartbeat_classification(monkeypatch):
    monkeypatch.setattr(
        provider_heartbeat,
        "_select_transports",
        lambda p, m, prefer_oauth: [
            TargetTransport(via="openrouter", base_url="x", api_key_env="K")
        ],
    )

    async def ok(*a, **k):
        return _cr(200)

    async def overloaded(*a, **k):
        return _cr(529)

    async def errored(*a, **k):
        return _cr(0, error="boom")

    monkeypatch.setattr(provider_heartbeat, "_run_canary", ok)
    assert (await provider_heartbeat.ping_once(None, "openrouter", "m", None))["status"] == "ok"
    monkeypatch.setattr(provider_heartbeat, "_run_canary", overloaded)
    assert (await provider_heartbeat.ping_once(None, "openrouter", "m", None))[
        "status"
    ] == "degraded"
    monkeypatch.setattr(provider_heartbeat, "_run_canary", errored)
    assert (await provider_heartbeat.ping_once(None, "openrouter", "m", None))["status"] == "down"


async def test_heartbeat_no_credential(monkeypatch):
    monkeypatch.setattr(provider_heartbeat, "_select_transports", lambda p, m, prefer_oauth: [])
    res = await provider_heartbeat.ping_once(None, "openai", "gpt-x", None)
    assert res["status"] == "no-cred"
