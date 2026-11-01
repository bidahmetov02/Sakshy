"""Tests run on the host against the compose `db` service (`docker compose up -d db`).

They use the separate `sakshy_test` database, created by `db/init/` on first start.
Override with TEST_DATABASE_URL. Each session rebuilds the schema from the migrations.
Probe and worker tests also need the compose `emulator` service (override: EMULATOR_URL),
and email tests the compose `mailpit` service (override: MAILPIT_URL, SMTP_HOST, SMTP_PORT).
"""

import asyncio
import json
import os
import urllib.request
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from api.main import create_app
from core.db import create_engine
from core.events import NOTIFY_CHANNEL
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
        database_url=PostgresDsn(TEST_DATABASE_URL),
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        jwt_secret=SecretStr("test-only-secret-" + "x" * 32),
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


@pytest.fixture
async def listener(settings: Settings) -> AsyncGenerator[asyncio.Queue[dict[str, Any]]]:
    """Check events (`pg_notify`) as they are committed."""
    url = str(settings.database_url).replace("+asyncpg", "")
    conn = await cast(Any, asyncpg).connect(url)  # asyncpg ships no type hints
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
        queue.put_nowait(json.loads(payload))

    await conn.add_listener(NOTIFY_CHANNEL, on_notify)
    yield queue
    await conn.close()


# ---------- API ----------

PASSWORD = "correct horse battery"


@pytest.fixture
async def app(engine: AsyncEngine, settings: Settings) -> AsyncGenerator[FastAPI]:
    """The API app with its lifespan running, on the emptied-after-each-test database."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


def api_client(app: FastAPI) -> httpx2.AsyncClient:
    # https, so the client sends back the Secure session cookie.
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://test")


@pytest.fixture
async def anon(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient]:
    async with api_client(app) as client:
        yield client


type SignIn = Callable[[str], Awaitable[httpx2.AsyncClient]]


@pytest.fixture
async def sign_in(app: FastAPI) -> AsyncGenerator[SignIn]:
    """Registers a user and returns a client holding their session cookie."""
    async with AsyncExitStack() as stack:

        async def sign_in(email: str) -> httpx2.AsyncClient:
            client = await stack.enter_async_context(api_client(app))
            body = {"email": email, "password": PASSWORD}
            response = await client.post("/api/auth/register", json=body)
            assert response.status_code == 201, response.text
            return client

        yield sign_in
