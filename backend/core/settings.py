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


@lru_cache
def get_settings() -> Settings:
    return Settings()
