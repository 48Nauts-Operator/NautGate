"""Week 2 — plugin contract (Concept §"plugin contract").

Five HTTP hooks NautGate calls into optional extension services:

1. ``before_route`` — synchronous, default 50ms timeout. Returns brain hints
   that can override the scored tier or extend banned_models. On timeout or
   error: skipped, default routing proceeds.
2. ``on_request`` — fire-and-forget after PRECAPTURE.
3. ``on_response`` — fire-and-forget after upstream returns.
4. ``after_route`` — fire-and-forget after the response is delivered to the client.
5. ``on_outcome`` — fire-and-forget after route_outcomes lands.

All hooks are POST <base_url>/v1/<hook_name> with a JSON body. Configured via
the ``extensions:`` block in ``nautgate.yaml``; absent / empty config → no-op.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

log = structlog.get_logger()

DEFAULT_TIMEOUT_MS = 200
BEFORE_ROUTE_DEFAULT_TIMEOUT_MS = 50

VALID_HOOKS = ("before_route", "on_request", "on_response", "after_route", "on_outcome")


@dataclass(frozen=True)
class Extension:
    name: str
    base_url: str
    hooks: tuple[str, ...] = ()
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    timeout_ms_before_route: int = BEFORE_ROUTE_DEFAULT_TIMEOUT_MS

    def supports(self, hook: str) -> bool:
        return hook in self.hooks


class PluginRegistry:
    """Holds the configured extensions + an httpx.AsyncClient for hook dispatch.

    A registry with no extensions is a no-op: every dispatch returns immediately
    without doing anything. That's the default in dev/test where nautgate.yaml
    ships with `extensions:` commented out.
    """

    def __init__(self, extensions: list[Extension] | None = None):
        self.extensions: list[Extension] = list(extensions or [])
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(2.0, connect=0.5),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            http2=False,
        )
        self._pending: set[asyncio.Task] = set()

    @classmethod
    def from_config(cls, path: Path | str | None) -> PluginRegistry:
        """Load extensions from ``nautgate.yaml``. Missing file → empty registry."""
        if path is None:
            return cls([])
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return cls([])
        except yaml.YAMLError as exc:
            log.warning("plugin_config_parse_failed", path=str(path), error=str(exc))
            return cls([])

        exts_raw = raw.get("extensions") or {}
        if not isinstance(exts_raw, dict):
            return cls([])

        exts: list[Extension] = []
        for name, body in exts_raw.items():
            if not isinstance(body, dict):
                continue
            base_url = body.get("base_url")
            if not isinstance(base_url, str) or not base_url:
                continue
            hooks = tuple(h for h in (body.get("hooks") or []) if h in VALID_HOOKS)
            exts.append(
                Extension(
                    name=str(name),
                    base_url=base_url.rstrip("/"),
                    hooks=hooks,
                    timeout_ms=int(body.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
                    timeout_ms_before_route=int(
                        body.get("timeout_ms_before_route", BEFORE_ROUTE_DEFAULT_TIMEOUT_MS)
                    ),
                )
            )
        return cls(exts)

    @property
    def is_empty(self) -> bool:
        return not self.extensions

    def subscribers(self, hook: str) -> list[Extension]:
        return [e for e in self.extensions if e.supports(hook)]

    async def aclose(self) -> None:
        # Wait briefly for any pending fire-and-forget tasks; cancel rest.
        if self._pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending, return_exceptions=True),
                    timeout=0.5,
                )
            except TimeoutError:
                for t in self._pending:
                    t.cancel()
        await self._client.aclose()

    # ---- before_route: synchronous fan-out + aggregate ----

    async def call_before_route(self, payload: dict) -> dict:
        """Fan out to before_route subscribers and aggregate their hints.

        Returns:
            {
                "brain_hints": dict,         # union of all hint dicts (later wins)
                "banned_models": list,       # union of all extension bans
                "preferred_tier": str|None,  # last-wins
                "override_model": str|None,  # last-wins (per Tech Paper §2.5 level 5)
                "demoted_models": list,      # union (level 6)
                "promoted_models": list,     # union (level 6)
            }
        """
        agg: dict[str, Any] = {
            "brain_hints": {},
            "banned_models": [],
            "preferred_tier": None,
            "override_model": None,
            "demoted_models": [],
            "promoted_models": [],
        }
        for ext in self.subscribers("before_route"):
            body = await self._call_with_timeout(
                ext, "before_route", payload, timeout_ms=ext.timeout_ms_before_route
            )
            if not isinstance(body, dict):
                continue
            hints = body.get("brain_hints")
            if isinstance(hints, dict):
                agg["brain_hints"].update(hints)
            for list_field in ("banned_models", "demoted_models", "promoted_models"):
                value = body.get(list_field)
                if isinstance(value, list):
                    agg[list_field].extend(v for v in value if isinstance(v, str))
            preferred = body.get("preferred_tier")
            if isinstance(preferred, str) and preferred:
                agg["preferred_tier"] = preferred
            override = body.get("override_model")
            if isinstance(override, str) and override:
                agg["override_model"] = override
        return agg

    async def _call_with_timeout(
        self,
        ext: Extension,
        hook: str,
        payload: dict,
        *,
        timeout_ms: int,
    ) -> dict | None:
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                resp = await self._client.post(f"{ext.base_url}/v1/{hook}", json=_jsonable(payload))
            if resp.status_code >= 400:
                log.info(
                    "plugin_hook_status_error",
                    ext=ext.name,
                    hook=hook,
                    status=resp.status_code,
                )
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        except TimeoutError:
            log.info("plugin_hook_timeout", ext=ext.name, hook=hook, timeout_ms=timeout_ms)
            return None
        except httpx.HTTPError as exc:
            log.info("plugin_hook_http_error", ext=ext.name, hook=hook, error=str(exc))
            return None

    # ---- fire-and-forget hooks ----

    def dispatch_on_request(self, payload: dict) -> None:
        self._fire_and_forget("on_request", payload)

    def dispatch_on_response(self, payload: dict) -> None:
        self._fire_and_forget("on_response", payload)

    def dispatch_after_route(self, payload: dict) -> None:
        self._fire_and_forget("after_route", payload)

    def dispatch_on_outcome(self, payload: dict) -> None:
        self._fire_and_forget("on_outcome", payload)

    def _fire_and_forget(self, hook: str, payload: dict) -> None:
        for ext in self.subscribers(hook):
            task = asyncio.create_task(
                self._call_with_timeout(ext, hook, payload, timeout_ms=ext.timeout_ms)
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)


def _jsonable(payload: dict) -> dict:
    """Make a payload JSON-serializable: UUIDs → strings, datetimes → isoformat."""
    import datetime
    import uuid

    def coerce(v):
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, datetime.datetime):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: coerce(x) for k, x in v.items()}
        if isinstance(v, list):
            return [coerce(x) for x in v]
        return v

    return coerce(payload)
