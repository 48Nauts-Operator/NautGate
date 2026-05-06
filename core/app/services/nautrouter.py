from collections.abc import AsyncIterator

import httpx
import structlog

log = structlog.get_logger()


class NautRouterClient:
    """HTTP keepalive client for the NautRouter sidecar.

    NautRouter speaks OpenAI Chat at POST /v1/chat/completions, with native streaming
    SSE. Day 2 wires the non-streaming path; Day 3 adds the streaming branch using
    the tee accumulator in app.streaming.
    """

    def __init__(self, base_url: str, *, timeout_s: float = 120.0) -> None:
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=2.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            http2=False,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def chat_completions(self, payload: dict) -> dict:
        """Non-streaming POST to NautRouter. Returns the parsed JSON body.

        The caller has already verified `payload.get("stream") is not True`.
        """
        resp = await self._client.post("/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            log.warning(
                "nautrouter_upstream_error",
                status=resp.status_code,
                body=resp.text[:500],
            )
            resp.raise_for_status()
        return resp.json()

    async def chat_completions_stream(self, payload: dict) -> AsyncIterator[bytes]:
        """Yield raw SSE bytes. Caller is responsible for the tee + accumulator (Day 3)."""
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            json={**payload, "stream": True},
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_raw():
                yield chunk
