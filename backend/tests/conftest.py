"""Tests run on the host against the compose `db` service (`docker compose up -d db`).

They use the separate `sakshy_test` database, created by `db/init/` on first start.
Override with TEST_DATABASE_URL. Each session rebuilds the schema from the migrations.
"""

import os
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

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sakshy:sakshy@localhost:5432/sakshy_test"
)
BACKEND_DIR = Path(__file__).parents[1]


def alembic_config(connection: Connection) -> Config:
    """Alembic config that runs migrations on `connection` (see `migrations/env.py`)."""
    config = Config(toml_file=BACKEND_DIR / "pyproject.toml")
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=PostgresDsn(TEST_DATABASE_URL))


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
    """The migrated test database, emptied after each test.

    TRUNCATE rather than a rolled-back transaction, so tests can use several connections.
    """
    yield migrated_engine
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with migrated_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
