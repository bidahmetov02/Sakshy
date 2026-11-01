"""The worker process end to end: real Postgres, real HTTP to the compose emulator, real
SMTP to the compose Mailpit."""

import asyncio
import os
import secrets
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import CheckResult, MonitorState, WorkerHeartbeat
from core.settings import Settings
from tests.factories import add_check, add_user, rows, state_of
from tests.mailpit import Mailpit, new_address
from worker import heartbeat, state_machine
from worker.housekeeping import daily_partitions, partition_name
from worker.main import run

BACKEND_DIR = Path(__file__).parents[1]


@pytest.fixture
def fast(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "worker_claim_interval_s": 0.05,
            "worker_heartbeat_interval_s": 0.05,
            "worker_outbox_interval_s": 0.05,
            "worker_sweep_interval_s": 0.05,
        }
    )


async def eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Poll the database: the worker runs in this loop but signals nothing we could await."""
    async with asyncio.timeout(5):
        while not await condition():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def test_worker_exits_when_stop_is_set(engine: AsyncEngine, settings: Settings) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_worker_probes_due_checks_and_records_results(
    engine: AsyncEngine, fast: Settings, emulator: str
) -> None:
    alice = await add_user(engine)
    up = await add_check(engine, alice, url=f"{emulator}/ok")
    failing = await add_check(engine, alice, url=f"{emulator}/status/503")

    stop = asyncio.Event()
    worker = asyncio.create_task(run(fast, stop))

    async def both_ran() -> bool:
        return len(await rows(engine, CheckResult)) >= 2

    await eventually(both_ran)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)

    up_state, failing_state = await state_of(engine, up), await state_of(engine, failing)
    assert up_state.state is MonitorState.UP
    assert up_state.lease_owner is None
    assert failing_state.state is MonitorState.FAILING
    assert failing_state.last_status_code == 503


async def test_heartbeat_while_running_and_removed_on_exit(
    engine: AsyncEngine, fast: Settings
) -> None:
    stop = asyncio.Event()
    worker = asyncio.create_task(run(fast, stop))

    async def registered() -> bool:
        return len(await rows(engine, WorkerHeartbeat)) == 1

    await eventually(registered)
    [first] = await rows(engine, WorkerHeartbeat)
    await asyncio.sleep(0.2)
    [later] = await rows(engine, WorkerHeartbeat)
    assert later.started_at == first.started_at
    assert later.last_seen_at > first.last_seen_at

    stop.set()
    await asyncio.wait_for(worker, timeout=5)
    assert await rows(engine, WorkerHeartbeat) == []


async def test_mark_alive_upserts_and_remove_deletes(engine: AsyncEngine) -> None:
    await heartbeat.mark_alive(engine, "w1")
    await heartbeat.mark_alive(engine, "w1")
    assert [h.worker_id for h in await rows(engine, WorkerHeartbeat)] == ["w1"]
    await heartbeat.remove(engine, "w1")
    assert await rows(engine, WorkerHeartbeat) == []


async def wait_until_leased(engine: AsyncEngine, check_id: int) -> None:
    async def leased() -> bool:
        return (await state_of(engine, check_id)).lease_owner is not None

    await eventually(leased)


async def test_shutdown_waits_for_in_flight_checks(
    engine: AsyncEngine, fast: Settings, emulator: str
) -> None:
    check_id = await add_check(engine, await add_user(engine), url=f"{emulator}/slow?ms=700")
    stop = asyncio.Event()
    worker = asyncio.create_task(run(fast, stop))
    await wait_until_leased(engine, check_id)

    stop.set()
    await asyncio.wait_for(worker, timeout=5)

    assert len(await rows(engine, CheckResult)) == 1
    assert (await state_of(engine, check_id)).lease_owner is None


async def test_shutdown_abandons_checks_still_running_after_the_grace_period(
    engine: AsyncEngine, fast: Settings, emulator: str
) -> None:
    check_id = await add_check(engine, await add_user(engine), url=f"{emulator}/hang")
    impatient = fast.model_copy(update={"worker_shutdown_grace_s": 0.2})
    stop = asyncio.Event()
    worker = asyncio.create_task(run(impatient, stop))
    await wait_until_leased(engine, check_id)

    stop.set()
    await asyncio.wait_for(worker, timeout=2)

    # No result: the run is a gap, and the lease expires for another worker to take over.
    assert await rows(engine, CheckResult) == []
    assert (await state_of(engine, check_id)).lease_owner is not None


async def test_worker_process_exits_cleanly_on_sigterm(
    engine: AsyncEngine, settings: Settings
) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "worker.main",
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": str(settings.database_url)},
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    try:
        async with asyncio.timeout(10):
            while b"worker started" not in (line := await proc.stderr.readline()):
                assert line, "worker exited before starting"
        proc.send_signal(signal.SIGTERM)
        async with asyncio.timeout(5):
            stderr = await proc.stderr.read()
            assert await proc.wait() == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert b"worker stopping" in stderr
    assert await rows(engine, WorkerHeartbeat) == []


async def test_worker_creates_todays_partition_before_claiming(
    engine: AsyncEngine, fast: Settings
) -> None:
    stop = asyncio.Event()
    worker = asyncio.create_task(run(fast, stop))

    async def registered() -> bool:
        return len(await rows(engine, WorkerHeartbeat)) == 1

    await eventually(registered)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)
    async with engine.connect() as conn:
        now = (await conn.execute(select(func.now()))).scalar_one()
        assert now.astimezone(UTC).date() in await daily_partitions(conn)


async def test_down_and_up_emails_end_to_end(
    engine: AsyncEngine,
    fast: Settings,
    emulator: str,
    mailpit: Mailpit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The site breaks, the worker confirms it down and emails; it recovers, UP follows."""
    monkeypatch.setattr(state_machine, "RETRY_DELAY", timedelta(milliseconds=50))
    site, to = f"e2e-{secrets.token_hex(4)}", new_address()
    async with httpx2.AsyncClient(base_url=emulator) as control:
        (await control.put(f"/control/sites/{site}", json={"mode": "error"})).raise_for_status()
        check_id = await add_check(
            engine,
            await add_user(engine),
            name="Shop",
            url=f"{emulator}/sites/{site}",
            interval_s=30,
            timeout_s=2,
            alert_emails=[to],
        )
        stop = asyncio.Event()
        worker = asyncio.create_task(run(fast, stop))
        try:
            [down] = await mailpit.wait_for(to, 1)
            assert down["Subject"] == "[Sakshy] DOWN: Shop"
            assert "status_mismatch: expected status 200, got 500" in down["Text"]

            (await control.delete(f"/control/sites/{site}")).raise_for_status()
            async with engine.begin() as conn:  # run now instead of in 30 s
                await conn.execute(
                    text("UPDATE check_state SET next_run_at = now() WHERE check_id = :id"),
                    {"id": check_id},
                )
            [_, up] = await mailpit.wait_for(to, 2)
            assert up["Subject"].startswith("[Sakshy] UP: Shop (down ")
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=5)

    assert (await state_of(engine, check_id)).state is MonitorState.UP
    async with engine.connect() as conn:
        tables = await conn.scalars(
            text("SELECT DISTINCT tableoid::regclass::text FROM check_results")
        )
        today = (await conn.execute(select(func.now()))).scalar_one().astimezone(UTC).date()
        assert set(tables) == {partition_name(today)}
