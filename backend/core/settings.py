from functools import lru_cache

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, read from environment variables (and `.env` for host dev).

    Defaults target the compose services as seen from the host, so `uv run ...`
    works against `docker compose up db` without any setup.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://sakshy:sakshy@localhost:5432/sakshy"
    )
    log_level: str = "INFO"

    # Worker
    worker_concurrency: int = 100  # max checks in flight; also the DNS thread pool size
    worker_claim_interval_s: float = 1.0
    worker_heartbeat_interval_s: float = 10.0
    worker_shutdown_grace_s: float = 10.0  # wait this long for in-flight checks on SIGTERM


@lru_cache
def get_settings() -> Settings:
    return Settings()
