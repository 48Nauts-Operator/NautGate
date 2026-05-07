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

    nautrouter_base_url: str = "http://localhost:8404"

    # CLASSIFY slow-path (Tech Paper §7.3). Off by default. When on, ambiguous
    # fast-path-"none" prompts get a one-shot LLM verify with a 500ms timeout.
    nautgate_classify_llm_confirm: bool = False
    nautgate_classify_llm_confirm_model: str = "claude-haiku-4-5"
    nautgate_classify_llm_confirm_timeout_s: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
