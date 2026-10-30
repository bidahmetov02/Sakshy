from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_connects_to_postgres_18_test_database(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SHOW server_version_num"))).scalar_one()
        database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    assert int(version) >= 180000
    assert database == "sakshy_test"
