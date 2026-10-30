import pytest

from core.settings import Settings


def test_reads_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    assert str(Settings(_env_file=None).database_url) == "postgresql+asyncpg://u:p@db:5432/x"  # pyright: ignore[reportCallIssue]


def test_rejects_non_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://u:p@db/x")
    with pytest.raises(ValueError, match="scheme"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
