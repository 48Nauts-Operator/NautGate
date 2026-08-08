"""CORS — browser clients (a builder UI on :3000) call the gateway cross-origin.

A different port is already a different origin, so without these headers the
browser refuses the call before it ever reaches us.
"""

import pytest


@pytest.mark.asyncio
async def test_preflight_allowed_for_localhost_origin(client):
    r = await client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.asyncio
async def test_simple_request_carries_allow_origin(client):
    r = await client.get("/health", headers={"Origin": "http://127.0.0.1:8080"})
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:8080"


@pytest.mark.asyncio
async def test_unknown_origin_is_not_allowed(client):
    """A random site the user visits must not be able to read the gateway."""
    r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
