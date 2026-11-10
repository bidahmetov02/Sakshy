from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import PostgresDsn, SecretStr, field_validator, model_validator
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
    # "starttls" (port 587) or "tls" (port 465) refuse to send unencrypted; "none" is for
    # Mailpit. Username and password go together, or neither.
    smtp_tls: Literal["none", "starttls", "tls"] = "none"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str = "Sakshy <alerts@sakshy.local>"
    app_base_url: str = "http://localhost:8080"  # for links in alerts

    # Telegram alerts to one chat, the operator's, for every check on the server: all
    # three or none. The owner's own checks ring; everyone else's arrive silently.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_owner_email: str | None = None  # the operator's Sakshy login
    telegram_api_url: str = "https://api.telegram.org"

    @field_validator("probe_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        return frozenset(h.strip().lower() for h in value if h.strip())

    @model_validator(mode="after")
    def _all_or_none(self) -> Self:
        smtp = {"SMTP_USERNAME": self.smtp_username, "SMTP_PASSWORD": self.smtp_password}
        telegram = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "TELEGRAM_OWNER_EMAIL": self.telegram_owner_email,
        }
        for group in (smtp, telegram):
            missing = [name for name, value in group.items() if not value]
            if 0 < len(missing) < len(group):
                raise ValueError(f"set {', '.join(group)} together; missing {', '.join(missing)}")
        return self

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
