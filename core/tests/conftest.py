import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

# Force tests to run with no DB URL — lifespan tolerates this and sets app.state.db = None.
os.environ.setdefault("NAUTGATE_DB_URL", "")
os.environ.setdefault("NAUTGATE_LOG_LEVEL", "WARNING")


@pytest.fixture
def empty_db_pool():
    """Asyncpg-shaped pool with no persisted rows.

    ``Pool.acquire()`` returns an async context manager rather than a coroutine.
    Modeling that distinction keeps post-response jobs honest and prevents
    AsyncMock-created coroutines from being discarded unawaited.
    """
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=None)
    pool.execute = AsyncMock(return_value=None)
    pool.close = AsyncMock(return_value=None)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    acquired = pool.acquire.return_value
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=None)
    return pool


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
