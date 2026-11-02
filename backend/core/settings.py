from functools import lru_cache
from typing import Annotated, Any

from pydantic import PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # API auth. The API refuses to start without a secret; the worker doesn't need one.
    jwt_secret: SecretStr | None = None  # HS256 key, at least 32 characters
    access_token_ttl_s: int = 24 * 3600  # a JWT can't be revoked: logout only drops the cookie
    cookie_secure: bool = True  # browsers accept Secure cookies on http://localhost

    # Worker
    worker_concurrency: int = 100  # max checks in flight; also the DNS thread pool size
    worker_claim_interval_s: float = 1.0
    worker_heartbeat_interval_s: float = 10.0
    worker_shutdown_grace_s: float = 10.0  # wait this long for in-flight checks on SIGTERM
    worker_outbox_interval_s: float = 2.0
    worker_sweep_interval_s: float = 15.0
    worker_housekeeping_interval_s: float = 3600.0
    rollup_retention_days: int = 90  # hourly rollups; raw results are kept 7 days
    # Hosts probes may reach even at private addresses, comma-separated (dev: "emulator").
    # The API reads it too, to accept such URLs when a check is saved.
    probe_allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset()

    # Alert emails
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_timeout_s: float = 10.0
    mail_from: str = "Sakshy <alerts@sakshy.local>"
    app_base_url: str = "http://localhost:8080"  # for links in emails

    @field_validator("probe_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        return frozenset(h.strip().lower() for h in value if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
