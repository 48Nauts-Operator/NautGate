from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI

from app.db.migrate import apply_migrations
from app.db.pool import open_pool
from app.logging_config import configure_logging
from app.routes import health, v1
from app.settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.nautgate_log_level)
    log = structlog.get_logger()

    app.state.settings = settings
    app.state.db = None
    app.state.nautrouter = None  # Day 2 wires the NautRouter HTTP sidecar client

    if settings.nautgate_db_url:
        try:
            pool = await open_pool(settings.nautgate_db_url)
            await apply_migrations(pool, MIGRATIONS_DIR)
            app.state.db = pool
            log.info("db_pool_ready", url_host=_redacted_host(settings.nautgate_db_url))
        except Exception as exc:
            log.error("db_pool_failed", error=str(exc))
    else:
        log.warning("no_db_url_configured", hint="set NAUTGATE_DB_URL to enable persistence")

    try:
        yield
    finally:
        if app.state.db is not None:
            await app.state.db.close()
            log.info("db_pool_closed")


def _redacted_host(url: str) -> str:
    try:
        return url.split("@", 1)[-1].split("/", 1)[0]
    except Exception:
        return "?"


def create_app() -> FastAPI:
    app = FastAPI(
        title="NautGate",
        version="0.1.0",
        description="Memory-aware LLM gateway",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(v1.router)
    return app


app = create_app()
