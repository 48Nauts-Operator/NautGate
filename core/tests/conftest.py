import os

import httpx
import pytest
from asgi_lifespan import LifespanManager

# Force tests to run with no DB URL — lifespan tolerates this and sets app.state.db = None.
os.environ.setdefault("NAUTGATE_DB_URL", "")
os.environ.setdefault("NAUTGATE_LOG_LEVEL", "WARNING")


@pytest.fixture
async def app():
    # Import inside the fixture so settings are read after env vars are set above.
    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        yield c
