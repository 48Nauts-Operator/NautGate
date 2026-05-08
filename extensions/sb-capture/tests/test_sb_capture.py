"""sb-capture tests — NDJSON + Postgres sinks + HTTP shape."""

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
    monkeypatch.setenv("SB_CAPTURE_SINK", "ndjson")
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


# --- HTTP shape ----------------------------------------------------------


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "NDJSONSink" in body["sinks"]


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


# --- Helpers --------------------------------------------------------------


def test_extract_last_user_text_string():
    from sinks import _extract_last_user_text

    body = json.dumps([{"role": "user", "content": "hello"}])
    assert _extract_last_user_text(body) == "hello"


def test_extract_last_user_text_block_list():
    from sinks import _extract_last_user_text

    body = json.dumps(
        [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "ok"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "image", "source": {}},
                    {"type": "text", "text": "second"},
                ],
            },
        ]
    )
    out = _extract_last_user_text(body)
    assert "first" in out and "second" in out


def test_extract_last_user_text_handles_garbage():
    from sinks import _extract_last_user_text

    assert _extract_last_user_text("not json at all") == ""
    assert _extract_last_user_text(None) == ""
    assert _extract_last_user_text("") == ""
    assert _extract_last_user_text("[]") == ""


def test_extract_assistant_text_openai_chat():
    from sinks import _extract_assistant_text

    body = json.dumps(
        {
            "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
            "usage": {},
        }
    )
    assert _extract_assistant_text(body) == "hi there"


def test_extract_assistant_text_openai_responses():
    from sinks import _extract_assistant_text

    body = json.dumps(
        {
            "output_text": "convenience field",
            "output": [
                {"content": [{"type": "output_text", "text": "structured"}]}
            ],
        }
    )
    # Convenience field wins.
    assert _extract_assistant_text(body) == "convenience field"


def test_extract_assistant_text_anthropic():
    from sinks import _extract_assistant_text

    body = json.dumps(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    )
    assert _extract_assistant_text(body) == "a\nb"


def test_extract_assistant_text_raw_string():
    from sinks import _extract_assistant_text

    assert _extract_assistant_text("plain assembled text") == "plain assembled text"


# --- PostgresSink (no real DB; verify SQL/method shape via stub) -----------


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _FakeAcquire(self.conn)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_postgres_sink_writes_user_message(monkeypatch):
    from sinks import PostgresSink

    sink = PostgresSink("postgres://test")
    pool = _FakePool()
    sink._pool = pool

    body = {
        "agent_id": "alice",
        "decision_id": "d1",
        "prompt_body": json.dumps([{"role": "user", "content": "summarize this design"}]),
        "model_requested": "auto",
        "decision_provider": "openai",
        "decision_model": "gpt-4o-mini",
        "classified_tier": "fast",
    }
    await sink.write("on_request", body)
    assert len(pool.conn.calls) == 1
    sql, args = pool.conn.calls[0]
    assert "INSERT INTO memories" in sql
    assert args[0] == "alice"  # agent_id
    assert args[1] == "user_message"  # category
    assert args[2] == "summarize this design"  # content


@pytest.mark.asyncio
async def test_postgres_sink_skips_short_content():
    from sinks import PostgresSink

    sink = PostgresSink("postgres://test")
    pool = _FakePool()
    sink._pool = pool

    # Short prompt → skipped.
    await sink.write(
        "on_request",
        {
            "agent_id": "alice",
            "decision_id": "d1",
            "prompt_body": json.dumps([{"role": "user", "content": "hi"}]),
        },
    )
    assert len(pool.conn.calls) == 0


@pytest.mark.asyncio
async def test_postgres_sink_writes_assistant_response():
    from sinks import PostgresSink

    sink = PostgresSink("postgres://test")
    pool = _FakePool()
    sink._pool = pool

    body = {
        "agent_id": "alice",
        "decision_id": "d1",
        "response_body": json.dumps(
            {
                "choices": [{"message": {"content": "the answer is 42"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4},
            }
        ),
        "status_code": 200,
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "was_empty": False,
    }
    await sink.write("on_response", body)
    assert len(pool.conn.calls) == 1
    sql, args = pool.conn.calls[0]
    assert args[1] == "assistant_response"
    assert args[2] == "the answer is 42"
