import pytest


@pytest.mark.asyncio
async def test_health_always_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body  # nav-chrome (NAUTGATE-13) surfaces the version here


@pytest.mark.asyncio
async def test_ready_returns_503_without_db(client):
    # Conftest sets NAUTGATE_DB_URL="" so the lifespan leaves app.state.db = None.
    resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert "reason" in body
