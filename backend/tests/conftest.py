"""Tests run on the host against the compose `db` service (`docker compose up -d db`).

They use the separate `sakshy_test` database, created by `db/init/` on first start.
Override with TEST_DATABASE_URL. Each session rebuilds the schema from the migrations.
Probe and worker tests also need the compose `emulator` service (override: EMULATOR_URL),
and email tests the compose `mailpit` service (override: MAILPIT_URL, SMTP_HOST, SMTP_PORT).
"""

import os
import urllib.request
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import PostgresDsn
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db import create_engine
from core.models import Base
from core.settings import Settings
from tests.mailpit import Mailpit

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sakshy:sakshy@localhost:5432/sakshy_test"
)
BACKEND_DIR = Path(__file__).parents[1]


def alembic_config(connection: Connection) -> Config:
    """Alembic config that runs migrations on `connection` (see `migrations/env.py`)."""
    config = Config(toml_file=BACKEND_DIR / "pyproject.toml")
    config.attributes["connection"] = connection
    return config


SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=PostgresDsn(TEST_DATABASE_URL), smtp_host=SMTP_HOST, smtp_port=SMTP_PORT
    )


@pytest.fixture(scope="session")
async def migrated_engine(settings: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(str(settings.database_url))
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(lambda c: command.upgrade(alembic_config(c), "head"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def engine(migrated_engine: AsyncEngine) -> AsyncGenerator[AsyncEngine]:
    """The migrated test database, emptied after each test, daily partitions included.

    TRUNCATE rather than a rolled-back transaction, so tests can use several connections.
    """
    yield migrated_engine
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with migrated_engine.begin() as conn:
        partitions = await conn.scalars(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " WHERE i.inhparent = 'check_results'::regclass"
                " AND c.relname <> 'check_results_default'"
            )
        )
        for name in partitions.all():
            await conn.execute(text(f"DROP TABLE {name}"))
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


EMULATOR_URL = os.environ.get("EMULATOR_URL", "http://127.0.0.1:9000")


@pytest.fixture(scope="session")
def emulator() -> str:
    """Base URL of the compose `emulator` service (`docker compose up -d --build emulator`)."""
    try:
        urllib.request.urlopen(f"{EMULATOR_URL}/ok", timeout=2).close()  # noqa: S310
    except OSError as exc:
        pytest.fail(f"emulator not reachable at {EMULATOR_URL}: {exc}")
    return EMULATOR_URL


MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://127.0.0.1:8025")


@pytest.fixture(scope="session")
def mailpit() -> Mailpit:
    """The compose `mailpit` service: its SMTP port for sending, its API for reading back."""
    try:
        urllib.request.urlopen(f"{MAILPIT_URL}/api/v1/info", timeout=2).close()  # noqa: S310
    except OSError as exc:
        pytest.fail(f"mailpit not reachable at {MAILPIT_URL}: {exc}")
    return Mailpit(MAILPIT_URL)
