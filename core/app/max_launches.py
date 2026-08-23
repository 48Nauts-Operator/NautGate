"""Short-lived, owner-local launch capabilities for Claude Code."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class MaxLaunch:
    app: str
    project: str
    native_session: str
    run_id: str
    owner_instance: str
    expires_at: float


_LAUNCHES: dict[str, MaxLaunch] = {}
DEFAULT_TTL_SECONDS = 6 * 60 * 60
MAX_TTL_SECONDS = 24 * 60 * 60


def register_launch(
    *,
    app: str,
    project: str,
    native_session: str,
    run_id: str,
    owner_instance: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, MaxLaunch]:
    current = time.monotonic() if now is None else now
    ttl = max(60, min(int(ttl_seconds), MAX_TTL_SECONDS))
    token = secrets.token_urlsafe(32)
    launch = MaxLaunch(
        app.strip(),
        project.strip(),
        native_session.strip(),
        run_id.strip(),
        owner_instance.strip(),
        current + ttl,
    )
    _LAUNCHES[token] = launch
    _prune(current)
    return token, launch


def validate_launch(token: str, *, now: float | None = None) -> MaxLaunch:
    current = time.monotonic() if now is None else now
    launch = _LAUNCHES.get(token)
    if launch is None or launch.expires_at <= current:
        _LAUNCHES.pop(token, None)
        raise HTTPException(status_code=401, detail="invalid_or_expired_max_launch")
    return launch


def bind_request(request: Request, token: str) -> MaxLaunch:
    launch = validate_launch(token)
    request.state.max_launch = launch
    request.state.project_id = launch.project or None
    request.state.anthropic_upstream_path = "/v1/messages"
    return launch


def clear_launches() -> None:
    _LAUNCHES.clear()


def _prune(now: float) -> None:
    for token, launch in list(_LAUNCHES.items()):
        if launch.expires_at <= now:
            _LAUNCHES.pop(token, None)
