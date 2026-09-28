"""Weight-file digests for locally served models (NAUTGATE-58).

A model name in a receipt is a label; for providers whose weights we can
inspect, the receipt also pins the manifest digest, so "which model" becomes
a cryptographic claim. Resolution is fail-open: no digest ever blocks or
delays an outcome write beyond the short timeout.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx

# ponytail: Ollama only; add LM Studio/vLLM sources when a deployment needs them.
_INSPECTABLE = {"ollama"}


class WeightsResolver:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._cache: dict[tuple[str, str], dict] = {}

    def _base_url(self) -> str:
        return os.environ.get("NAUTGATE_OLLAMA_URL", "http://localhost:11434")

    async def resolve(self, provider: str | None, model: str | None) -> dict:
        """Return weights evidence fields, or {} when nothing could be pinned."""
        if provider not in _INSPECTABLE or not model:
            return {}
        key = (provider, model)
        if key in self._cache:
            return self._cache[key]
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=0.5, base_url=self._base_url()
            ) as client:
                resp = await client.get("/api/tags")
                resp.raise_for_status()
                models = resp.json().get("models") or []
        except Exception:
            return {}
        digest = next((m.get("digest") for m in models if m.get("name") == model), None)
        if not digest:
            return {}
        evidence = {
            "weights_digest": f"sha256:{digest}",
            "weights_digest_source": "ollama-manifest",
            "weights_resolved_at": datetime.now(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        }
        self._cache[key] = evidence
        return evidence


weights_resolver = WeightsResolver()
