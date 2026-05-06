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

    nautrouter_base_url: str = "http://localhost:8404"


@lru_cache
def get_settings() -> Settings:
    return Settings()
