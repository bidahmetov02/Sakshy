"""Worker entrypoint: claims due checks, probes them, records the results; sends alert
emails, runs the maintenance sweep and housekeeping.

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
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db import create_engine
from core.log import configure_logging
from core.settings import Settings, get_settings
from worker import heartbeat, housekeeping, outbox, runs, sweep
from worker.probe import create_client, probe

log = logging.getLogger("worker")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _every(
    stop: asyncio.Event, seconds: float, job: Callable[[], Awaitable[object]], name: str
) -> None:
    """Run `job` every `seconds` until stopped. A failed round is logged and retried."""
    while not stop.is_set():
        await _sleep_or_stop(stop, seconds)
        if stop.is_set():
            break
        try:
            await job()
        except Exception:
            log.exception("%s failed", name)


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        client: httpx2.AsyncClient,
        send: outbox.Send,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.client = client
        self.send = send
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

    async def housekeep(self) -> None:
        await housekeeping.run(self.engine, self.settings.rollup_retention_days)

    def background(self, stop: asyncio.Event) -> list[asyncio.Task[None]]:
        """The periodic jobs beside the claim loop."""
        s = self.settings
        jobs: list[tuple[float, Callable[[], Awaitable[object]], str]] = [
            (
                s.worker_heartbeat_interval_s,
                lambda: heartbeat.mark_alive(self.engine, self.id),
                "heartbeat",
            ),
            (
                s.worker_outbox_interval_s,
                lambda: outbox.send_due(self.engine, s, self.send, stop),
                "outbox",
            ),
            (s.worker_sweep_interval_s, lambda: sweep.sweep(self.engine), "maintenance sweep"),
            (s.worker_housekeeping_interval_s, self.housekeep, "housekeeping"),
        ]
        return [asyncio.create_task(_every(stop, *job)) for job in jobs]

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


async def run(settings: Settings, stop: asyncio.Event, send: outbox.Send | None = None) -> None:
    engine = create_engine(str(settings.database_url))
    try:
        async with create_client() as client:
            worker = Worker(settings, engine, client, send or outbox.smtp_sender(settings))
            await heartbeat.mark_alive(engine, worker.id)
            # Before claiming, so today's results partition exists.
            try:
                await worker.housekeep()
            except Exception:
                log.exception("housekeeping failed")
            spread = await runs.spread_overdue_call_times(engine)
            log.info("worker started: %s, spread %d overdue checks", worker.id, spread)
            background = worker.background(stop)
            await worker.claim_loop(stop)
            log.info("worker stopping: %d checks in flight", len(worker.in_flight))
            await worker.drain()
            await asyncio.gather(*background)
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
