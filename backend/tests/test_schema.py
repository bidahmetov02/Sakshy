"""The migrations and the ORM models describe the same schema."""

from typing import Any

from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.models import Base
from tests.conftest import alembic_config

MODELS_SCHEMA = "models_check"

# Each query lists one aspect of a schema's tables as comparable rows. Partitions are
# skipped (the worker creates them at runtime), and so is alembic_version (not a model).
_TABLES = "pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
_ONLY_MODEL_TABLES = """
    n.nspname = :schema AND c.relkind IN ('r', 'p')
    AND NOT c.relispartition AND c.relname <> 'alembic_version'
"""
CATALOG_QUERIES = {
    "tables": f"""
        SELECT c.relname, c.relkind, c.reloptions, pg_get_partkeydef(c.oid)
        FROM {_TABLES} WHERE {_ONLY_MODEL_TABLES}
    """,
    "columns": f"""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               a.attidentity, pg_get_expr(d.adbin, d.adrelid)
        FROM {_TABLES}
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE {_ONLY_MODEL_TABLES}
    """,
    "constraints": f"""
        SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM {_TABLES} JOIN pg_constraint con ON con.conrelid = c.oid
        WHERE {_ONLY_MODEL_TABLES}
    """,
    "indexes": f"""
        SELECT c.relname, i.relname, pg_get_indexdef(i.oid)
        FROM {_TABLES}
        JOIN pg_index x ON x.indrelid = c.oid JOIN pg_class i ON i.oid = x.indexrelid
        WHERE {_ONLY_MODEL_TABLES}
    """,
    "enums": """
        SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE n.nspname = :schema GROUP BY t.typname
    """,
}


async def describe(conn: AsyncConnection, schema: str) -> dict[str, set[tuple[Any, ...]]]:
    # With only `schema` on the search path, catalog functions print its objects unqualified,
    # so two schemas with the same definitions describe identically.
    await conn.execute(text(f"SET LOCAL search_path TO {schema}"))
    result: dict[str, set[tuple[Any, ...]]] = {}
    for aspect, query in CATALOG_QUERIES.items():
        rows = (await conn.execute(text(query), {"schema": schema})).all()
        result[aspect] = {
            tuple(None if v is None else str(v).replace(f"{schema}.", "") for v in row)
            for row in rows
        }
    return result


async def test_models_match_migrated_catalog(engine: AsyncEngine) -> None:
    """Also catches what autogenerate ignores: CHECKs, partial indexes, storage params."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text(f"CREATE SCHEMA {MODELS_SCHEMA}"))
        await conn.execute(text(f"SET LOCAL search_path TO {MODELS_SCHEMA}"))
        await conn.run_sync(Base.metadata.create_all)
        from_models = await describe(conn, MODELS_SCHEMA)
        from_migrations = await describe(conn, "public")
        await conn.rollback()
    assert len(from_migrations["tables"]) == len(Base.metadata.tables)
    for aspect in CATALOG_QUERIES:
        assert from_models[aspect] == from_migrations[aspect], aspect


async def test_autogenerate_detects_no_changes(engine: AsyncEngine) -> None:
    def check(connection: Connection) -> None:
        command.check(alembic_config(connection))  # raises if a migration would be generated

    async with engine.connect() as conn:
        await conn.run_sync(check)


async def test_downgrade_removes_everything_then_upgrade_restores(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        # A runtime partition must not block the downgrade.
        await conn.execute(
            text(
                "CREATE TABLE check_results_20261030 PARTITION OF check_results"
                " FOR VALUES FROM ('2026-10-30 00:00+00') TO ('2026-10-31 00:00+00')"
            )
        )
        await conn.run_sync(lambda c: command.downgrade(alembic_config(c), "base"))
        tables = await conn.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        enums = await conn.scalars(
            text(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                " WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        )
        assert (tables.all(), enums.all()) == (["alembic_version"], [])
        await conn.run_sync(lambda c: command.upgrade(alembic_config(c), "head"))
        assert len((await describe(conn, "public"))["tables"]) == len(Base.metadata.tables)
