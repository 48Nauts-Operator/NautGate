from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI

from app.db import queries
from app.db.migrate import apply_migrations
from app.db.pool import open_pool
from app.logging_config import configure_logging
from app.routes import health, v1
from app.services.nautrouter import NautRouterClient
from app.settings import get_settings
from app.spool import OutcomeSpool

MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.nautgate_log_level)
    log = structlog.get_logger()

    app.state.settings = settings
    app.state.db = None
    app.state.nautrouter = None
    app.state.outcome_spool = OutcomeSpool(settings.nautgate_outcome_spool_path)

    if settings.nautgate_db_url:
        try:
            pool = await open_pool(settings.nautgate_db_url)
            await apply_migrations(pool, MIGRATIONS_DIR)
            app.state.db = pool
            log.info("db_pool_ready", url_host=_redacted_host(settings.nautgate_db_url))
            try:
                result = await app.state.outcome_spool.drain(queries.write_outcome, pool)
                if result.drained or result.pending or result.skipped_bad:
                    log.info(
                        "outcome_spool_drain_on_startup",
                        drained=result.drained,
                        pending=result.pending,
                        skipped_bad=result.skipped_bad,
                    )
            except Exception as exc:
                log.warning("outcome_spool_drain_failed", error=str(exc))
        except Exception as exc:
            log.error("db_pool_failed", error=str(exc))
    else:
        log.warning("no_db_url_configured", hint="set NAUTGATE_DB_URL to enable persistence")

    if settings.nautrouter_base_url:
        app.state.nautrouter = NautRouterClient(settings.nautrouter_base_url)
        log.info("nautrouter_client_ready", base_url=settings.nautrouter_base_url)

    try:
        yield
    finally:
        if app.state.nautrouter is not None:
            await app.state.nautrouter.aclose()
            log.info("nautrouter_client_closed")
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
