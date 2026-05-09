from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import queries
from app.db.migrate import apply_migrations
from app.db.pool import open_pool
from app.logging_config import configure_logging
from app.plugins import PluginRegistry
from app.provider_health import ProviderHealthTracker
from app.routes import health, v1
from app.scoring import load_routing_table
from app.services.nautrouter import NautRouterClient
from app.settings import get_settings
from app.spool import OutcomeSpool

MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"
DEFAULT_ROUTING_CONFIG = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.nautgate_log_level)
    log = structlog.get_logger()

    app.state.settings = settings
    app.state.db = None
    app.state.nautrouter = None
    app.state.outcome_spool = OutcomeSpool(settings.nautgate_outcome_spool_path)
    app.state.health_tracker = ProviderHealthTracker()
    app.state.plugins = PluginRegistry.from_config(settings.nautgate_config_path)
    if not app.state.plugins.is_empty:
        log.info(
            "plugins_loaded",
            count=len(app.state.plugins.extensions),
            names=[e.name for e in app.state.plugins.extensions],
        )

    # Day 5a/b: tier → provider/model routing table for `model: "auto"`.
    routing_path = Path(settings.nautgate_routing_config_path or DEFAULT_ROUTING_CONFIG)
    try:
        app.state.routing_table = load_routing_table(routing_path)
        log.info("routing_table_loaded", path=str(routing_path), tiers=len(app.state.routing_table))
    except Exception as exc:
        log.warning("routing_table_load_failed", path=str(routing_path), error=str(exc))
        app.state.routing_table = None

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
        if getattr(app.state, "plugins", None) is not None:
            await app.state.plugins.aclose()
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

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/dashboard")
        async def dashboard_index() -> FileResponse:
            return FileResponse(str(static_dir / "index.html"))

        @app.get("/")
        async def root_redirect() -> RedirectResponse:
            return RedirectResponse(url="/dashboard", status_code=302)

    return app


app = create_app()
