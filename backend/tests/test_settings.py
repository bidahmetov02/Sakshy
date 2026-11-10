import pytest

from core.settings import Settings


def test_reads_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    assert str(Settings(_env_file=None).database_url) == "postgresql+asyncpg://u:p@db:5432/x"  # pyright: ignore[reportCallIssue]


def test_rejects_non_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://u:p@db/x")
    with pytest.raises(ValueError, match="scheme"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"SMTP_USERNAME": "me@gmail.com"}, "SMTP_PASSWORD"),
        ({"TELEGRAM_BOT_TOKEN": "123:abc"}, "TELEGRAM_CHAT_ID, TELEGRAM_OWNER_EMAIL"),
        ({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}, "TELEGRAM_OWNER_EMAIL"),
    ],
)
def test_credentials_come_all_or_none(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], missing: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"missing {missing}"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_telegram_is_on_with_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not Settings(_env_file=None).telegram_enabled  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_OWNER_EMAIL", "me@x.io")
    assert Settings(_env_file=None).telegram_enabled  # pyright: ignore[reportCallIssue]


def test_rejects_an_unknown_smtp_tls_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_TLS", "ssl")
    with pytest.raises(ValueError, match="smtp_tls"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
