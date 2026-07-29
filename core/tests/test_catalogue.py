"""Model catalogue — the picker must offer everything the providers serve, and
keep doing so without anyone maintaining a hardcoded list."""

import time

import pytest

from app.catalogue import (
    ModelCatalogue,
    ProviderResult,
    _fetch_openai,
    _fetch_openrouter,
    _is_chat_like,
)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _Client:
    """Stand-in for httpx.AsyncClient as an async context manager."""

    def __init__(self, resp=None, raise_exc=None):
        self._resp, self._raise = resp, raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if self._raise:
            raise self._raise
        return self._resp


# ---- filtering -----------------------------------------------------------


def test_non_chat_models_are_excluded():
    # A model picker that offers an embedding model is offering a broken choice.
    for bad in ("text-embedding-3-large", "bge-reranker", "whisper-1", "tts-1", "dall-e-3"):
        assert not _is_chat_like(bad)
    for good in ("gpt-4o", "claude-opus-4-8", "moonshotai/kimi-k2"):
        assert _is_chat_like(good)


@pytest.mark.asyncio
async def test_openai_allowlists_chat_families(monkeypatch):
    payload = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "o3-mini"},
            {"id": "chatgpt-4o-latest"},
            {"id": "text-embedding-3-large"},  # not chat
            {"id": "dall-e-3"},  # not chat
            {"id": "davinci-002"},  # base model, not a chat family
        ]
    }
    monkeypatch.setattr("app.catalogue.httpx.AsyncClient", lambda **k: _Client(_Resp(payload)))
    pr, items = await _fetch_openai("sk-test")
    ids = {m["id"] for m in items}
    assert ids == {"gpt-4o", "o3-mini", "chatgpt-4o-latest"}
    assert pr.ok and pr.count == 3
    # Bare ids so they take the passthrough lane.
    assert all("/" not in i for i in ids)


@pytest.mark.asyncio
async def test_openrouter_ids_are_prefixed_for_routing(monkeypatch):
    payload = {
        "data": [
            {"id": "moonshotai/kimi-k2", "name": "Kimi K2", "context_length": 262144},
            {"id": "openai/gpt-4o", "architecture": {"modality": "text->text"}},
            {"id": "some/image-only", "architecture": {"modality": "image->image"}},
        ]
    }
    monkeypatch.setattr("app.catalogue.httpx.AsyncClient", lambda **k: _Client(_Resp(payload)))
    pr, items = await _fetch_openrouter(None)  # public — works with no key
    ids = {m["id"] for m in items}
    assert ids == {"openrouter/moonshotai/kimi-k2", "openrouter/openai/gpt-4o"}
    assert pr.ok
    assert next(m for m in items if "kimi" in m["id"])["nautgate_context"] == 262144


# ---- fail-soft -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_outage_costs_only_that_provider(monkeypatch):
    monkeypatch.setattr(
        "app.catalogue.httpx.AsyncClient", lambda **k: _Client(raise_exc=RuntimeError("boom"))
    )
    pr, items = await _fetch_openrouter(None)
    assert items == [] and pr.ok is False and pr.error == "RuntimeError"


@pytest.mark.asyncio
async def test_missing_key_skips_provider_without_erroring():
    pr, items = await _fetch_openai(None)
    assert items == [] and pr.error == "no_key" and pr.ok is False


# ---- cache / refresh -----------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_merges_dedupes_and_stamps(monkeypatch):
    async def fake_or(_k):
        return ProviderResult("openrouter", count=2, ok=True), [
            {"id": "openrouter/a", "nautgate_provider": "openrouter"},
            {"id": "openrouter/a", "nautgate_provider": "openrouter"},  # dup
        ]

    async def empty(*a, **k):
        return ProviderResult("x", ok=True), []

    monkeypatch.setattr("app.catalogue._fetch_openrouter", fake_or)
    monkeypatch.setattr("app.catalogue._fetch_anthropic", empty)
    monkeypatch.setattr("app.catalogue._fetch_openai", empty)
    monkeypatch.setattr("app.catalogue._fetch_lmstudio", empty)

    cat = ModelCatalogue()
    assert cat.is_stale  # nothing fetched yet
    status = await cat.refresh()
    assert status["count"] == 1  # de-duplicated on id
    assert not cat.is_stale
    assert cat.fetched_at is not None


@pytest.mark.asyncio
async def test_goes_stale_after_the_ttl(monkeypatch):
    async def empty(*a, **k):
        return ProviderResult("x", ok=True), []

    for f in ("_fetch_openrouter", "_fetch_anthropic", "_fetch_openai", "_fetch_lmstudio"):
        monkeypatch.setattr(f"app.catalogue.{f}", empty)

    cat = ModelCatalogue(ttl_seconds=24 * 3600)
    await cat.refresh()
    assert not cat.is_stale
    # 24h later the picker must go looking for newly released models again.
    cat._fetched_at = time.time() - (24 * 3600 + 1)
    assert cat.is_stale


def test_default_ttl_is_daily():
    assert ModelCatalogue().ttl_seconds == 24 * 60 * 60
