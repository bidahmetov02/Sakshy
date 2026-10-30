"""Worker entrypoint: claims due checks, probes them, records the results.

The worker coordinates with other workers only through the database, so any number of
them can run side by side (`docker compose up --scale worker=3`).
"""

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db import create_engine
from core.log import configure_logging
from core.settings import Settings, get_settings
from worker import heartbeat, runs
from worker.probe import create_client, probe

log = logging.getLogger("worker")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class Worker:
    def __init__(self, settings: Settings, engine: AsyncEngine, client: httpx2.AsyncClient) -> None:
        self.settings = settings
        self.engine = engine
        self.client = client
        self.id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(2)}"
        self.in_flight: set[asyncio.Task[None]] = set()

    async def claim_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # Claim no more than there are free slots, so a claimed check starts at once.
            free = self.settings.worker_concurrency - len(self.in_flight)
            if free > 0:
                claimed_mono = time.monotonic()
                try:
                    claims = await runs.claim(self.engine, self.id, free)
                except Exception:
                    log.exception("claim failed")
                    claims = []
                for claim in claims:
                    task = asyncio.create_task(self.run_check(claim, claimed_mono))
                    self.in_flight.add(task)
                    task.add_done_callback(self.in_flight.discard)
            await _sleep_or_stop(stop, self.settings.worker_claim_interval_s)

    async def run_check(self, claim: runs.Claim, claimed_mono: float) -> None:
        try:
            # The probe start on the database clock: claim time plus local elapsed time.
            checked_at = claim.claimed_at + timedelta(seconds=time.monotonic() - claimed_mono)
            result = await probe(
                self.client, claim.url, claim.timeout_s, claim.expected_status, claim.expected_body
            )
            outcome = await runs.complete(self.engine, self.id, claim, result, checked_at)
            if outcome is not runs.Completion.RECORDED:
                log.info("check %d: result %s", claim.check_id, outcome.value)
        except Exception:
            # The lease expires and another run picks the check up.
            log.exception("check %d: run failed", claim.check_id)

    async def heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _sleep_or_stop(stop, self.settings.worker_heartbeat_interval_s)
            try:
                await heartbeat.mark_alive(self.engine, self.id)
            except Exception:
                log.exception("heartbeat failed")

    async def drain(self) -> None:
        """Wait for in-flight checks, then cancel the rest: their leases simply expire."""
        if not self.in_flight:
            return
        _, pending = await asyncio.wait(
            set(self.in_flight), timeout=self.settings.worker_shutdown_grace_s
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            log.warning("abandoned %d in-flight checks", len(pending))


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(str(settings.database_url))
    try:
        async with create_client() as client:
            worker = Worker(settings, engine, client)
            await heartbeat.mark_alive(engine, worker.id)
            spread = await runs.spread_overdue_call_times(engine)
            log.info("worker started: %s, spread %d overdue checks", worker.id, spread)
            heartbeats = asyncio.create_task(worker.heartbeat_loop(stop))
            await worker.claim_loop(stop)
            log.info("worker stopping: %d checks in flight", len(worker.in_flight))
            await worker.drain()
            await heartbeats
            await heartbeat.remove(engine, worker.id)
    finally:
        await engine.dispose()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    loop = asyncio.get_running_loop()
    # DNS lookups run in the default executor: size it for every probe in flight.
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=settings.worker_concurrency, thread_name_prefix="dns")
    )
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


if __name__ == "__main__":
    asyncio.run(main())
