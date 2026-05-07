"""sb-capture writes one NDJSON line per hook invocation."""

import json
import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def output_path(tmp_path, monkeypatch):
    p = tmp_path / "events.ndjson"
    monkeypatch.setenv("SB_CAPTURE_OUTPUT_PATH", str(p))
    return p


@pytest.fixture
async def client(output_path):
    from main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sb-capture.test"
        ) as c:
            yield c


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,hook",
    [
        ("/v1/on_request", "on_request"),
        ("/v1/on_response", "on_response"),
        ("/v1/on_outcome", "on_outcome"),
        ("/v1/after_route", "after_route"),
    ],
)
async def test_each_hook_appends_one_line(client, output_path, path, hook):
    payload = {"decision_id": "abc-123", "agent_id": "alice"}
    resp = await client.post(path, json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    lines = output_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["hook"] == hook
    assert parsed["payload"] == payload
    assert isinstance(parsed["received_at"], float)


@pytest.mark.asyncio
async def test_multiple_calls_grow_file(client, output_path):
    for i in range(3):
        await client.post("/v1/on_request", json={"i": i})
    lines = output_path.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["payload"]["i"] for line in lines] == [0, 1, 2]


@pytest.mark.asyncio
async def test_rejects_non_object_body(client):
    resp = await client.post("/v1/on_request", json=["not", "an", "object"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rejects_invalid_json(client):
    resp = await client.post(
        "/v1/on_request",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
