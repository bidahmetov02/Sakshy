"""Tests run on the host against the compose `db` service (`docker compose up -d db`).

They use the separate `sakshy_test` database, created by `db/init/` on first start.
Override with TEST_DATABASE_URL.
"""

import os
from collections.abc import AsyncGenerator

import pytest
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db import create_engine
from core.settings import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sakshy:sakshy@localhost:5432/sakshy_test"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=PostgresDsn(TEST_DATABASE_URL))


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(str(settings.database_url))
    yield engine
    await engine.dispose()
