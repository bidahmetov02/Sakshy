"""Worker entrypoint. Stage 1 stub: connects to the DB and idles until SIGTERM/SIGINT."""

import asyncio
import logging
import signal

from sqlalchemy import text

from core.db import create_engine
from core.log import configure_logging
from core.settings import Settings, get_settings

log = logging.getLogger("worker")


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(str(settings.database_url))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("worker started")
        await stop.wait()
        log.info("worker stopping")
    finally:
        await engine.dispose()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


if __name__ == "__main__":
    asyncio.run(main())
