"""Model catalogue — the full list of models you can actually pick.

`/v1/models` used to be composed only from `config/routing.yaml` (each tier's
primary + fallback), so the Settings → Keys picker offered about eight models
while the providers between them serve hundreds. This module fetches each
provider's OWN catalogue and caches it, so the list is complete and — the point
— **stays current on its own**. No hardcoded model list to update by hand every
time a provider ships something new.

Refreshed every 24h by a scheduler, and lazily on first use. Every fetch is
fail-soft and per-provider: no key, a down provider or a schema change costs you
that provider's entries, never the whole list.

Model ids follow the conventions in `scoring.py`:
  openrouter → ``openrouter/<vendor>/<model>``   (routed via NautRouter)
  anthropic  → bare ``claude-…``                 (OAuth/subscription passthrough)
  openai     → bare ``gpt-…`` / ``o…``           (OAuth/subscription passthrough)
  lmstudio   → ``lmstudio/<id>``                 (local)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx
import structlog

log = structlog.get_logger()

DEFAULT_TTL_SECONDS = 24 * 60 * 60

# OpenAI's /v1/models includes embeddings, audio, image and moderation models
# that cannot serve a chat completion. Allow-list the chat families instead of
# denying an ever-growing list of everything else.
_OPENAI_CHAT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4", "o5")

# Nothing that cannot answer a chat completion belongs in a model picker.
# "vision" is deliberately absent — vision models do answer chat completions.
_NON_CHAT_HINTS = (
    "embed",
    "rerank",
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "guard",
    "image",
    "audio",
    "realtime",
    "transcribe",
    "sora",
    "video",
)


def _is_chat_like(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(h in lowered for h in _NON_CHAT_HINTS)


@dataclass
class ProviderResult:
    """Outcome of one provider fetch — surfaced so the UI can be honest about
    which providers are actually contributing to the list."""

    provider: str
    count: int = 0
    ok: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"provider": self.provider, "count": self.count, "ok": self.ok}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class ModelCatalogue:
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    _models: list[dict] = field(default_factory=list)
    _providers: list[ProviderResult] = field(default_factory=list)
    _fetched_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---- state ----------------------------------------------------------

    @property
    def fetched_at(self) -> float | None:
        return self._fetched_at

    @property
    def is_stale(self) -> bool:
        return self._fetched_at is None or (time.time() - self._fetched_at) > self.ttl_seconds

    def models(self) -> list[dict]:
        return list(self._models)

    def status(self) -> dict:
        return {
            "fetched_at": self._fetched_at,
            "age_seconds": None if self._fetched_at is None else time.time() - self._fetched_at,
            "stale": self.is_stale,
            "count": len(self._models),
            "providers": [p.to_dict() for p in self._providers],
        }

    # ---- refresh --------------------------------------------------------

    async def refresh(self, *, keys: dict[str, str] | None = None, force: bool = False) -> dict:
        """Refetch every provider catalogue. Concurrent, fail-soft, idempotent.

        ``keys`` maps provider → API key; a provider with no key is simply
        skipped (OpenRouter's catalogue is public, so it works without one).
        """
        if not force and not self.is_stale:
            return self.status()
        async with self._lock:
            if not force and not self.is_stale:  # another caller won the race
                return self.status()
            keys = keys or {}
            results = await asyncio.gather(
                _fetch_openrouter(keys.get("openrouter")),
                _fetch_anthropic(keys.get("anthropic")),
                _fetch_openai(keys.get("openai")),
                _fetch_lmstudio(),
                return_exceptions=True,
            )
            models: list[dict] = []
            providers: list[ProviderResult] = []
            for res in results:
                if isinstance(res, BaseException):  # a fetcher itself blew up
                    providers.append(ProviderResult("unknown", ok=False, error=str(res)))
                    continue
                pr, items = res
                providers.append(pr)
                models.extend(items)
            # Deterministic order, and de-duplicated on id so a model offered by
            # two routes appears once.
            by_id: dict[str, dict] = {}
            for m in models:
                by_id.setdefault(m["id"], m)
            self._models = sorted(by_id.values(), key=lambda m: (m["nautgate_provider"], m["id"]))
            self._providers = providers
            self._fetched_at = time.time()
            log.info(
                "model_catalogue_refreshed",
                total=len(self._models),
                providers={p.provider: p.count for p in providers},
            )
            return self.status()


# --- per-provider fetchers -------------------------------------------------
# Each returns (ProviderResult, items) and never raises.


def _entry(model_id: str, provider: str, **extra) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "owned_by": provider,
        "nautgate_provider": provider,
        "nautgate_tiers": [],
        "nautgate_source": "catalogue",
        **extra,
    }


async def _fetch_openrouter(key: str | None) -> tuple[ProviderResult, list[dict]]:
    """OpenRouter's catalogue is public — no key required, so this populates
    even on a fresh install with nothing configured."""
    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                return ProviderResult("openrouter", error=f"http_{r.status_code}"), []
            items = (r.json() or {}).get("data") or []
    except Exception as exc:
        return ProviderResult("openrouter", error=type(exc).__name__), []

    out = []
    for m in items:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid or not _is_chat_like(mid):
            continue
        # Skip anything that cannot take text in and give text out.
        modality = ((m.get("architecture") or {}).get("modality") or "").lower()
        if modality and "text" not in modality:
            continue
        out.append(
            _entry(
                f"openrouter/{mid}",
                "openrouter",
                nautgate_context=(m.get("context_length") or None),
                nautgate_display=m.get("name") or mid,
            )
        )
    return ProviderResult("openrouter", count=len(out), ok=True), out


async def _fetch_anthropic(key: str | None) -> tuple[ProviderResult, list[dict]]:
    """Bare `claude-…` ids so they take the OAuth/subscription passthrough lane."""
    if not key:
        return ProviderResult("anthropic", error="no_key"), []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(
                "https://api.anthropic.com/v1/models?limit=1000",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            if r.status_code != 200:
                return ProviderResult("anthropic", error=f"http_{r.status_code}"), []
            items = (r.json() or {}).get("data") or []
    except Exception as exc:
        return ProviderResult("anthropic", error=type(exc).__name__), []

    out = [
        _entry(m["id"], "anthropic", nautgate_display=m.get("display_name") or m["id"])
        for m in items
        if isinstance(m, dict) and m.get("id") and _is_chat_like(m["id"])
    ]
    return ProviderResult("anthropic", count=len(out), ok=True), out


async def _fetch_openai(key: str | None) -> tuple[ProviderResult, list[dict]]:
    """Bare `gpt-…` / `o…` ids for the passthrough lane. OpenAI's list mixes in
    embeddings, audio and image models, so allow-list the chat families."""
    if not key:
        return ProviderResult("openai", error="no_key"), []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code != 200:
                return ProviderResult("openai", error=f"http_{r.status_code}"), []
            items = (r.json() or {}).get("data") or []
    except Exception as exc:
        return ProviderResult("openai", error=type(exc).__name__), []

    out = []
    for m in items:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid or not _is_chat_like(mid):
            continue
        if not mid.lower().startswith(_OPENAI_CHAT_PREFIXES):
            continue
        # gpt-3.5-turbo-instruct is a completions model, not a chat one. Only
        # OpenAI names it this way; on OpenRouter "instruct" means a normal chat
        # model (mistral-7b-instruct and friends), so this stays local to OpenAI.
        if "-instruct" in mid.lower():
            continue
        out.append(_entry(mid, "openai"))
    return ProviderResult("openai", count=len(out), ok=True), out


async def _fetch_lmstudio() -> tuple[ProviderResult, list[dict]]:
    """Locally-loaded LM Studio models. Usually not running, hence fail-soft.

    This is the HOST-reachable URL, distinct from LMSTUDIO_BASE_URL which
    NautRouter uses from inside its container (host.docker.internal).
    """
    base = os.environ.get("NAUTGATE_LMSTUDIO_URL", "http://localhost:1238").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as c:
            r = await c.get(f"{base}/v1/models")
            if r.status_code != 200:
                return ProviderResult("lmstudio", error=f"http_{r.status_code}"), []
            items = (r.json() or {}).get("data") or []
    except Exception as exc:
        return ProviderResult("lmstudio", error=type(exc).__name__), []

    out = [
        _entry(f"lmstudio/{m['id']}", "lmstudio")
        for m in items
        if isinstance(m, dict) and m.get("id") and _is_chat_like(m["id"])
    ]
    return ProviderResult("lmstudio", count=len(out), ok=True), out


# --- scheduler -------------------------------------------------------------


async def run_scheduler(catalogue: ModelCatalogue, key_resolver, *, is_offline=None) -> None:
    """Refresh the catalogue every TTL. Stands down while offline.

    Sleeps in short slices rather than one long sleep so a shutdown does not
    wait up to a day to be noticed.
    """
    tick = 300.0  # 5 min
    while True:
        try:
            offline = bool(is_offline()) if callable(is_offline) else False
            if not offline and catalogue.is_stale:
                await catalogue.refresh(keys=await key_resolver())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a scheduler must never die
            log.warning("model_catalogue_refresh_failed", error=str(exc))
        await asyncio.sleep(tick)
