from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    nautgate_db_url: str | None = None
    nautgate_listen_host: str = "0.0.0.0"
    nautgate_listen_port: int = 8090
    nautgate_log_level: str = "INFO"
    nautgate_profile: str = "auto"
    nautgate_config_path: str | None = None

    # Day 4d: durable-spool fallback for route_outcomes when Postgres is down.
    # Container deploys override this via NAUTGATE_OUTCOME_SPOOL_PATH=/var/lib/nautgate/spool/...
    nautgate_outcome_spool_path: str = "/tmp/nautgate/outcomes.ndjson"  # noqa: S108

    # Day 5a/b: tier → provider/model table for `model: "auto"`.
    # Defaults to <repo>/config/routing.yaml when run from the repo; container deploys
    # override via NAUTGATE_ROUTING_CONFIG_PATH=/etc/nautgate/routing.yaml.
    nautgate_routing_config_path: str | None = None
    nautgate_compliance_config_path: str | None = None

    # Compliance AUDIT trace (NAUTGATE-25). Observational — it records what a
    # call touched and never gates one. Off disables the trace write entirely.
    nautgate_compliance_trace: bool = True

    # Provider/model pricing. Defaults to <repo>/config/pricing.yaml.
    nautgate_pricing_config_path: str | None = None

    nautrouter_base_url: str = "http://localhost:8404"

    # Model catalogue: fetch each provider's own model list so the picker
    # offers everything they actually serve, and refresh it on a timer so new
    # models appear without anyone editing a hardcoded list.
    nautgate_model_catalogue: bool = True
    nautgate_model_catalogue_ttl_h: float = 24.0

    # Browser clients (a local builder UI, a dashboard on another port) are
    # cross-origin even on the same host — a different port is a different
    # origin. localhost/127.0.0.1 on any port is allowed out of the box; list
    # deployed origins here, comma-separated, or "*" to allow all.
    nautgate_cors_origins: str = ""

    # Captured prompt/response bodies are dropped after this many days; the row,
    # cost, model and attestation are kept forever. 0 disables pruning. Bodies
    # are the entire size problem: route_decisions hit 13 GB of a 14 GB database.
    nautgate_body_retention_days: int = 90

    # Read timeout for upstream model calls, in seconds. A long report or a
    # thinking model can legitimately run for many minutes, and the old
    # hard-coded 120s aborted work that was still in progress and reported it
    # as `502 upstream_failed`. Connect stays fast (2s) so a genuinely down
    # sidecar still fails immediately.
    nautgate_upstream_timeout_s: float = 600.0

    # CLASSIFY slow-path (Tech Paper §7.3). Off by default. When on, ambiguous
    # fast-path-"none" prompts get a one-shot LLM verify with a 500ms timeout.
    nautgate_classify_llm_confirm: bool = False
    nautgate_classify_llm_confirm_model: str = "claude-haiku-4-5"
    nautgate_classify_llm_confirm_timeout_s: float = 0.5

    # Dashboard auto-auth: when set, the /dashboard HTML embeds this token in a
    # <meta> tag so the JS skips the manual entry. Single-operator local use only;
    # never set on a multi-tenant or internet-exposed deploy.
    nautgate_local_admin_token: str | None = None

    # Shared secret the nautproxy sidecar presents (X-Ingest-Token) to POST
    # captured turns to /v1/ingest. Unset → the ingest endpoint is disabled.
    nautgate_ingest_token: str | None = None

    # Opt-in harness module (NAUTGATE-24): when true, the Anthropic Messages
    # bridge promotes a local model's text/reasoning pseudo tool call
    # (<tool_call>{...}</tool_call>) into structured tool_calls, so an agentic
    # harness (Claude Code) doesn't stall on an "empty answer". Off by default —
    # the default bridge path is unchanged.
    nautgate_harness_normalize: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
