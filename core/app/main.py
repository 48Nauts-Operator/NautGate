from contextlib import asynccontextmanager
from pathlib import Path

# Load .env into os.environ early so app modules that read via os.environ.get
# (sb_memory.py, backup.py, etc.) pick up values from .env without needing
# pydantic-settings declarations for every knob. Looks in core/.env first
# (where you typically launch uvicorn from) then repo root.
try:
    from dotenv import load_dotenv

    _here = Path(__file__).resolve().parent.parent  # → core/
    # Search core/.env, repo-root .env, deploy/.env — apply ALL that exist
    # (no break), with later ones not overriding earlier so the explicit
    # one wins. Provider keys (ANTHROPIC_API_KEY, OPENAI_API_KEY,
    # OPENROUTER_API_KEY) typically live in deploy/.env alongside the
    # docker-compose definition that needs them.
    for _p in (
        _here / ".env",
        _here.parent / ".env",
        _here.parent / "deploy" / ".env",
    ):
        if _p.is_file():
            load_dotenv(_p, override=False)
except ImportError:
    pass

import os as _os

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import crypto
from app.catalogue import ModelCatalogue
from app.compliance import load_policy as load_compliance_policy
from app.db import queries
from app.db.migrate import apply_migrations
from app.db.pool import open_pool
from app.logging_config import configure_logging
from app.plugins import PluginRegistry
from app.pricing import PricingTable
from app.provider_health import ProviderHealthTracker
from app.routes import health, v1
from app.scoring import load_routing_table
from app.services.nautrouter import NautRouterClient
from app.settings import get_settings
from app.spool import OutcomeSpool
from app.version import get_version

MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"


def _is_loopback(request: Request) -> bool:
    """True only when the request came from this machine.

    Proxy headers are deliberately ignored: X-Forwarded-For is caller-supplied
    and trusting it here would hand the token to anyone willing to set it.
    """
    host = getattr(request.client, "host", None) if request.client else None
    return host in ("127.0.0.1", "::1", "localhost")


def _bundled_config(name: str) -> Path:
    """Locate a bundled config file in either layout we actually ship.

    From a checkout the package sits at ``<repo>/core/app`` and the configs at
    ``<repo>/config`` — two levels up. In the Docker image the package is at
    ``/app/app`` and the configs at ``/app/config`` — only ONE level up, because
    the image drops the ``core/`` directory. Hardcoding ``parents[2]`` resolved
    to ``/config`` inside the image, which does not exist, so every published
    container silently ran with no pricing (all costs NULL), no routing table
    (``model: auto`` broken) and no compliance policy. Check both.
    """
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1]):
        candidate = base / "config" / name
        if candidate.exists():
            return candidate
    # Nothing found — return the checkout path so the failure names a real place.
    return here.parents[2] / "config" / name


DEFAULT_ROUTING_CONFIG = _bundled_config("routing.yaml")
DEFAULT_COMPLIANCE_CONFIG = _bundled_config("compliance.yaml")
DEFAULT_PRICING_CONFIG = _bundled_config("pricing.yaml")


async def _bootstrap_first_run_key(pool) -> None:
    """On an empty database, mint one API key and print it to the log.

    A fresh `docker compose up` has no key, and POST /v1/keys requires an
    existing one — there is no bootstrap endpoint. So on first run (zero keys)
    we mint one and print it prominently. The secret only ever touches the
    container log, whose access boundary is "you own this deployment". Once any
    key exists this is a no-op. Never fatal.
    """
    log = structlog.get_logger()
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM nautgate.api_keys")
        if count:
            return
        key = await queries.create_api_key(
            pool, name="first-run", agent_id="first-run", ttl_days=None
        )
        token = key["token"]
        # Print the key ALONE on its own line — no box, no decoration — so it
        # copy-pastes cleanly and `grep -oE 'ng_[a-f0-9]{32}_...'` extracts it whole.
        banner = (
            "\n"
            "  NautGate first-run API key — shown once, store it now:\n"
            "\n"
            f"{token}\n"
            "\n"
            "  Use as:  Authorization: Bearer <key>   ·   mint/revoke in Settings -> Keys\n"
        )
        print(banner, flush=True)
        log.warning("first_run_key_minted", agent_id="first-run")
    except Exception as exc:  # never block startup on this
        log.warning(
            "first_run_key_bootstrap_failed",
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )


class _RevalidatingStatic(StaticFiles):
    """Serve /static with `Cache-Control: no-cache`.

    Without it browsers heuristically cache these files for a long time, so an
    edited app.js or style.css keeps serving stale to anyone who has the page
    open — the mtime `?v=` bust only helps if the browser bothers to re-request.
    `no-cache` means "revalidate", not "don't cache": the ETag still yields a
    304, so this costs a conditional request, not a re-download.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.nautgate_log_level)
    log = structlog.get_logger()

    app.state.settings = settings
    app.state.db = None
    app.state.nautrouter = None
    app.state.chatgpt_subscription = None
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
        log.warning(
            "routing_table_load_failed",
            path=str(routing_path),
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
        app.state.routing_table = None

    # Compliance AUDIT policy (NAUTGATE-25) — jurisdiction scope, provider
    # registry, activity patterns, flag rules. Purely observational: it decides
    # what gets recorded and flagged, never whether a call proceeds.
    compliance_path = Path(settings.nautgate_compliance_config_path or DEFAULT_COMPLIANCE_CONFIG)
    try:
        app.state.compliance_policy = load_compliance_policy(compliance_path)
        log.info(
            "compliance_policy_loaded",
            path=str(compliance_path),
            evaluated_against=app.state.compliance_policy.evaluated_against(),
        )
    except Exception as exc:
        log.warning(
            "compliance_policy_load_failed",
            path=str(compliance_path),
            error=str(exc) or repr(exc),
            error_type=type(exc).__name__,
        )
        app.state.compliance_policy = None

    # Model catalogue — the full, self-updating list of selectable models.
    app.state.model_catalogue = (
        ModelCatalogue(ttl_seconds=settings.nautgate_model_catalogue_ttl_h * 3600.0)
        if settings.nautgate_model_catalogue
        else None
    )

    # Pricing config — feeds the per-outcome cost calculation.
    pricing_path = Path(settings.nautgate_pricing_config_path or DEFAULT_PRICING_CONFIG)
    app.state.pricing = PricingTable.from_yaml(pricing_path)
    log.info("pricing_table_loaded", path=str(pricing_path), models=app.state.pricing.size)

    # Guarantee a master key so in-app provider keys work with zero config (no .env).
    crypto.ensure_master_key()

    if settings.nautgate_db_url:
        try:
            pool = await open_pool(settings.nautgate_db_url)
            await apply_migrations(pool, MIGRATIONS_DIR)
            app.state.db = pool
            log.info("db_pool_ready", url_host=_redacted_host(settings.nautgate_db_url))
            await _bootstrap_first_run_key(pool)
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
                log.warning(
                    "outcome_spool_drain_failed",
                    error=str(exc) or repr(exc),
                    error_type=type(exc).__name__,
                )
        except Exception as exc:
            log.error("db_pool_failed", error=str(exc) or repr(exc), error_type=type(exc).__name__)
    else:
        log.warning("no_db_url_configured", hint="set NAUTGATE_DB_URL to enable persistence")

    if settings.nautrouter_base_url:
        app.state.nautrouter = NautRouterClient(
            settings.nautrouter_base_url,
            timeout_s=settings.nautgate_upstream_timeout_s,
        )
        log.info("nautrouter_client_ready", base_url=settings.nautrouter_base_url)

    if settings.nautgate_chatgpt_subscription_cli:
        try:
            from app.chatgpt_subscription import CodexSubscriptionClient

            app.state.chatgpt_subscription = CodexSubscriptionClient(
                executable=settings.nautgate_codex_cli_path,
                codex_home=settings.nautgate_codex_home,
                workdir=settings.nautgate_subscription_workdir,
                timeout_s=settings.nautgate_upstream_timeout_s,
            )
            log.info(
                "chatgpt_subscription_ready",
                transport="codex-cli",
                codex_home_configured=bool(settings.nautgate_codex_home),
            )
        except Exception as exc:
            # Fail closed at startup: a missing CLI must not cause explicit GPT
            # traffic to leak into the metered NautRouter OpenAI provider.
            log.error(
                "chatgpt_subscription_unavailable",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )

    # Quality-eval judge client. Direct httpx to OpenAI (or LMStudio when
    # the operator picks that in Settings) — intentionally bypasses
    # NautRouter so judge calls never get re-routed, never appear in our
    # own routing analytics, and don't create a feedback loop.
    import httpx as _httpx

    app.state.quality_judge = _httpx.AsyncClient(
        timeout=_httpx.Timeout(15.0, connect=2.0),
        limits=_httpx.Limits(max_keepalive_connections=4, max_connections=8),
    )

    # Background backup scheduler: ticks every minute, fires a backup when
    # backup_config.next_run_at has passed.
    app.state.backup_task = None
    app.state.retention_task = None
    app.state.llm_probe_task = None
    app.state.heartbeat_task = None
    app.state.audit_checkpoint_task = None
    app.state.audit_signer_task = None
    # Live provider-status heartbeat results, keyed by transport label.
    app.state.provider_status = {}
    # NAUTGATE_OFFLINE=1 — air-gapped deployments (on-prem, local models only).
    # The heartbeat and probe schedulers call out to api.anthropic.com /
    # openrouter.ai on a timer regardless of where traffic is actually routed,
    # so on an isolated box they produce a steady outbound beacon to providers
    # that aren't being used. Serving local models needs neither.

    app.state.offline = _os.environ.get("NAUTGATE_OFFLINE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if app.state.db is not None:
        import asyncio as _asyncio

        # Backup is local-only (writes to disk) — it runs in offline mode too.
        from app.backup import run_scheduler as _backup_scheduler

        app.state.backup_task = _asyncio.create_task(_backup_scheduler(app.state.db))

        from app import retention as _retention

        app.state.retention_task = _asyncio.create_task(
            _retention.run_scheduler(
                app.state.db, retention_days=settings.nautgate_body_retention_days
            )
        )

        if settings.nautgate_verified_audit_trail:
            from app.audit_worker import run_scheduler as _audit_checkpoint_scheduler

            app.state.audit_checkpoint_task = _asyncio.create_task(
                _audit_checkpoint_scheduler(
                    app.state.db,
                    instance_id=settings.nautgate_audit_instance_id,
                    signing_key_id=settings.nautgate_audit_signing_key_id,
                    max_receipts=settings.nautgate_audit_batch_size,
                    max_age_seconds=settings.nautgate_audit_batch_max_age_s,
                    tick_seconds=settings.nautgate_audit_tick_s,
                )
            )
            if settings.nautgate_audit_attest_url and settings.nautgate_audit_attest_token:
                from app.audit_signer import run_scheduler as _audit_signer_scheduler

                app.state.audit_signer_task = _asyncio.create_task(
                    _audit_signer_scheduler(
                        app.state.db,
                        sidecar_url=settings.nautgate_audit_attest_url,
                        internal_token=settings.nautgate_audit_attest_token,
                        expected_key_id=settings.nautgate_audit_signing_key_id,
                        expected_fingerprint=settings.nautgate_audit_public_key_fingerprint,
                        max_attempts=settings.nautgate_audit_sign_max_attempts,
                        tick_seconds=settings.nautgate_audit_tick_s,
                    )
                )
            else:
                log.warning(
                    "verified_audit_signer_not_configured",
                    hint="set NAUTGATE_AUDIT_ATTEST_URL and NAUTGATE_AUDIT_ATTEST_TOKEN",
                )

        # Both schedulers check app_config.is_offline() each tick and stand
        # down while offline, so the Settings toggle takes effect live — no
        # restart, which is what makes it demonstrable in front of an audience.
        if app.state.offline:
            log.info(
                "offline_mode_forced_by_env",
                hint="NAUTGATE_OFFLINE=1 — no outbound provider calls on a timer",
            )

        # LLM-Probing scheduler — disabled by default in config, so this idles
        # until the operator enables it + sets targets on the dashboard.
        from app.llm_probe_scheduler import run_scheduler as _probe_scheduler

        app.state.llm_probe_task = _asyncio.create_task(
            _probe_scheduler(
                app.state.db, pricing=app.state.pricing, judge_client=app.state.quality_judge
            )
        )

        # Active provider-status heartbeat (60s) → app.state.provider_status.
        from app.provider_heartbeat import run_scheduler as _heartbeat_scheduler

        app.state.heartbeat_task = _asyncio.create_task(
            _heartbeat_scheduler(
                app.state.db, pricing=app.state.pricing, state=app.state.provider_status
            )
        )

        # Model catalogue refresh. Providers ship new models constantly, so the
        # picker refetches on a timer (24h by default) instead of anyone editing
        # a hardcoded list. Stands down while offline like the other schedulers.
        if app.state.model_catalogue is not None:
            from app.catalogue import run_scheduler as _catalogue_scheduler

            async def _catalogue_keys() -> dict:
                """Provider keys for catalogue fetches: env first, then the
                encrypted store the dashboard writes to."""
                out: dict[str, str] = {}
                for prov, env in (
                    ("openrouter", "OPENROUTER_API_KEY"),
                    ("anthropic", "ANTHROPIC_API_KEY"),
                    ("openai", "OPENAI_API_KEY"),
                ):
                    val = _os.environ.get(env)
                    if not val and app.state.db is not None:
                        try:
                            val = await queries.get_provider_credential(app.state.db, prov)
                        except Exception:
                            val = None
                    if val:
                        out[prov] = val
                return out

            app.state.catalogue_keys = _catalogue_keys
            app.state.catalogue_task = _asyncio.create_task(
                _catalogue_scheduler(
                    app.state.model_catalogue,
                    _catalogue_keys,
                    is_offline=lambda: bool(getattr(app.state, "offline", False)),
                )
            )

    try:
        yield
    finally:
        for _tname in (
            "backup_task",
            "llm_probe_task",
            "heartbeat_task",
            "catalogue_task",
            "retention_task",
            "audit_checkpoint_task",
            "audit_signer_task",
        ):
            _t = getattr(app.state, _tname, None)
            if _t is not None:
                _t.cancel()
                try:
                    await _t
                except BaseException:
                    pass
        if getattr(app.state, "plugins", None) is not None:
            await app.state.plugins.aclose()
        try:
            from app.sb_memory import close_pool as _sb_close_pool

            await _sb_close_pool()
        except Exception:
            pass
        if getattr(app.state, "quality_judge", None) is not None:
            try:
                await app.state.quality_judge.aclose()
            except Exception:
                pass
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
        version=get_version(),
        description="Memory-aware LLM gateway",
        lifespan=lifespan,
    )
    # A browser refuses a cross-origin call unless the server opts in, and a
    # different port already counts as cross-origin — so any browser-based
    # client talking to the gateway needs this. Bearer-token auth (not cookies)
    # means allow_credentials stays off, so an allowed origin still cannot act
    # as the user without holding a key.
    cors = get_settings().nautgate_cors_origins
    origins = [o.strip() for o in cors.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in origins else origins,
        allow_origin_regex=None
        if "*" in origins
        else r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(v1.router)
    # Isolated Pi-only OpenAI Responses passthrough (POST /pi/v1/responses).
    # Additive; touches no existing route. See app/pi_responses.py.
    from app import pi_responses

    app.include_router(pi_responses.router)

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", _RevalidatingStatic(directory=str(static_dir)), name="static")

        # In-process cache for index.html — keyed by the mtime of index.html AND
        # of every asset whose mtime it stamps into a `?v=` query. Keying on
        # index.html alone meant editing app.js or style.css left the cached HTML
        # in place, so the page kept advertising the OLD `?v=` and browsers went
        # on serving stale JS/CSS until index.html happened to change too.
        _index_cache: dict[str, str | tuple] = {"key": (), "html": ""}

        def _asset_key() -> tuple:
            out = []
            for name in ("index.html", "style.css", "app.js", "kit.js"):
                try:
                    out.append(int((static_dir / name).stat().st_mtime))
                except OSError:
                    out.append(0)
            return tuple(out)

        def _read_index() -> str:
            key = _asset_key()
            if key == (0, 0, 0, 0):
                return _index_cache.get("html") or ""
            if key != _index_cache["key"]:
                _index_cache["key"] = key
                _index_cache["html"] = (static_dir / "index.html").read_text(encoding="utf-8")
            return _index_cache["html"]

        # Prime the cache so the first request doesn't pay the read.
        _read_index()

        @app.get("/dashboard")
        async def dashboard_index(request: Request) -> HTMLResponse:
            # Cache-bust the static assets by appending mtime-based query
            # strings. Without this the browser keeps serving stale CSS/JS
            # after each deploy and visual changes appear to "not land".
            settings = get_settings()
            try:
                css_v = int((static_dir / "style.css").stat().st_mtime)
                js_v = int((static_dir / "app.js").stat().st_mtime)
                kit_v = int((static_dir / "kit.js").stat().st_mtime)
            except OSError:
                css_v = js_v = kit_v = 0
            index_html = _read_index()
            html = (
                index_html.replace(
                    'href="/static/style.css"',
                    f'href="/static/style.css?v={css_v}"',
                )
                .replace(
                    'src="/static/app.js"',
                    f'src="/static/app.js?v={js_v}"',
                )
                .replace(
                    'src="/static/kit.js"',
                    f'src="/static/kit.js?v={kit_v}"',
                )
            )
            # If a local admin token is configured, inject it into a <meta> tag
            # so the JS skips manual entry. Token is server-rendered into the
            # HTML body — never travels via URL or cookie.
            #
            # ONLY for loopback callers. The setting is documented "local
            # single-operator use only", but that was never enforced: the server
            # binds 0.0.0.0, so anyone on the LAN or tailnet could GET /dashboard,
            # scrape this token out of the HTML and drive the whole API with it —
            # the audit log, prompts, costs and key management. Verified by doing
            # exactly that over the tailnet address. Remote browsers now enter a
            # key manually, which the dashboard already supports.
            token_meta = ""
            if settings.nautgate_local_admin_token and _is_loopback(request):
                t = settings.nautgate_local_admin_token.replace('"', "&quot;")
                token_meta = f'\n  <meta name="nautgate-token" content="{t}">'
            html = html.replace(
                "<title>NautGate</title>",
                f"<title>NautGate</title>{token_meta}",
            )
            return HTMLResponse(html)

        @app.get("/")
        async def root_redirect() -> RedirectResponse:
            return RedirectResponse(url="/dashboard", status_code=302)

    return app


app = create_app()
