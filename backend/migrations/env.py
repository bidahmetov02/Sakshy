import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from core.log import configure_logging
from core.models import Base
from core.settings import get_settings

config = context.config


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    # Daily check_results partitions are created by the worker at runtime, not by migrations.
    is_partition = type_ == "table" and (name or "").startswith("check_results_")
    return not (is_partition and reflected and compare_to is None)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_object=include_object,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_async_engine(str(settings.database_url), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline mode is not supported")

# Tests pass an open connection so migrations run inside their own event loop.
if (connection := config.attributes.get("connection")) is not None:
    do_run_migrations(connection)
else:
    asyncio.run(run_async_migrations())
