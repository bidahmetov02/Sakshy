"""Claim and completion against real Postgres: leases, fencing, the completion transaction,
incidents, outbox rows and the lock-order rule (design/sequences.md (a), (d), (f))."""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import (
    AlertKind,
    Check,
    CheckResult,
    CheckState,
    CheckStatsHourly,
    ErrorKind,
    Incident,
    IncidentEndReason,
    MonitorState,
    Notification,
)
from core.settings import Settings
from tests.factories import add_check, add_group, add_user, add_window, rows, state_of
from worker.probe import ProbeResult
from worker.runs import (
    NOTIFY_CHANNEL,
    Claim,
    Completion,
    claim,
    complete,
    spread_overdue_call_times,
)
from worker.state_machine import RETRY_DELAY

W1, W2 = "w1", "w2"
OK = ProbeResult(ok=True, response_ms=120, status_code=200)
FAIL = ProbeResult(ok=False, error_kind=ErrorKind.TIMEOUT, error_message="no response within 10 s")


@pytest.fixture
async def alice(engine: AsyncEngine) -> int:
    return await add_user(engine)


async def sql(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def make_due(engine: AsyncEngine, check_id: int) -> None:
    await sql(
        engine, "UPDATE check_state SET next_run_at = now() WHERE check_id = :id", id=check_id
    )


async def expire_lease(engine: AsyncEngine, check_id: int) -> None:
    await sql(
        engine,
        "UPDATE check_state SET lease_until = now() - interval '1 s' WHERE check_id = :id",
        id=check_id,
    )


async def claim_one(engine: AsyncEngine, check_id: int, worker: str = W1) -> Claim:
    claims = await claim(engine, worker, 100)
    assert [c.check_id for c in claims] == [check_id]
    return claims[0]


async def run(
    engine: AsyncEngine, check_id: int, result: ProbeResult, worker: str = W1
) -> Completion:
    await make_due(engine, check_id)
    c = await claim_one(engine, check_id, worker)
    return await complete(engine, worker, c, result, c.claimed_at)


async def db_now(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.now()))).scalar_one()


# ---------- Claim ----------


async def test_claim_leases_a_due_check(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice, url="https://a.example", expected_body="hi")
    c = await claim_one(engine, check_id)

    assert (c.url, c.timeout_s, c.expected_status, c.expected_body) == (
        "https://a.example",
        10.0,
        200,
        "hi",
    )
    state = await state_of(engine, check_id)
    assert state.lease_owner == W1
    assert state.lease_until == c.lease_until
    # Lease = the check's timeout + 5 s, on the database clock.
    assert c.lease_until - c.claimed_at == timedelta(seconds=15)


async def test_claim_skips_future_paused_deleted_and_leased_checks(
    engine: AsyncEngine, alice: int
) -> None:
    await add_check(engine, alice)
    await sql(engine, "UPDATE check_state SET next_run_at = now() + interval '1 min'")
    await add_check(engine, alice, is_paused=True)
    deleted = await add_check(engine, alice)
    await sql(engine, "UPDATE checks SET deleted_at = now() WHERE id = :id", id=deleted)
    leased = await add_check(engine, alice)
    await claim_one(engine, leased)  # asserts it is the only one claimed

    assert await claim(engine, W2, 100) == []  # and it is leased now


async def test_expired_lease_can_be_claimed_again(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    await claim_one(engine, check_id, W1)
    await expire_lease(engine, check_id)
    await claim_one(engine, check_id, W2)
    assert (await state_of(engine, check_id)).lease_owner == W2


async def test_claim_takes_the_most_overdue_first_up_to_the_limit(
    engine: AsyncEngine, alice: int
) -> None:
    ids = [await add_check(engine, alice) for _ in range(3)]
    for minutes, check_id in zip([1, 3, 2], ids, strict=True):
        await sql(
            engine,
            "UPDATE check_state SET next_run_at = now() - make_interval(mins => :m)"
            " WHERE check_id = :id",
            m=minutes,
            id=check_id,
        )
    claims = await claim(engine, W1, 2)
    assert [c.check_id for c in claims] == [ids[1], ids[2]]


async def test_concurrent_workers_claim_disjoint_checks(engine: AsyncEngine, alice: int) -> None:
    ids = {await add_check(engine, alice) for _ in range(30)}
    batches = await asyncio.gather(*(claim(engine, f"w{i}", 30) for i in range(4)))
    claimed = [c.check_id for batch in batches for c in batch]
    assert sorted(claimed) == sorted(ids)  # each check exactly once


# ---------- Completion: the normal path ----------


async def test_success_records_result_rollup_and_state(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    c = await claim_one(engine, check_id)
    scheduled = (await state_of(engine, check_id)).next_run_at
    assert await complete(engine, W1, c, OK, c.claimed_at) is Completion.RECORDED

    [result] = await rows(engine, CheckResult)
    assert (result.check_id, result.checked_at, result.ok, result.response_ms) == (
        check_id,
        c.claimed_at,
        True,
        120,
    )
    assert not result.is_retry

    [rollup] = await rows(engine, CheckStatsHourly)
    assert rollup.bucket == c.claimed_at.replace(minute=0, second=0, microsecond=0)
    assert (rollup.total, rollup.ok, rollup.rt_count, rollup.rt_sum_ms) == (1, 1, 1, 120)
    assert (rollup.rt_min_ms, rollup.rt_max_ms) == (120, 120)

    state = await state_of(engine, check_id)
    assert state.state is MonitorState.UP
    assert (state.lease_owner, state.lease_until) == (None, None)
    assert (state.last_checked_at, state.last_ok, state.last_status_code) == (
        c.claimed_at,
        True,
        200,
    )
    assert state.last_response_ms == 120
    assert state.next_run_at == scheduled + timedelta(seconds=60)  # anchored


async def test_rollup_accumulates_within_the_hour(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    await run(engine, check_id, OK)
    await run(engine, check_id, ProbeResult(ok=True, response_ms=300, status_code=200))
    await run(engine, check_id, ProbeResult(ok=True, response_ms=None, status_code=200))

    [rollup] = await rows(engine, CheckStatsHourly)
    assert (rollup.total, rollup.ok, rollup.rt_count, rollup.rt_sum_ms) == (3, 3, 2, 420)
    assert (rollup.rt_min_ms, rollup.rt_max_ms) == (120, 300)


async def test_failure_schedules_a_retry_and_retries_skip_the_rollup(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await run(engine, check_id, FAIL)
    state = await state_of(engine, check_id)
    assert (state.state, state.consecutive_failures) == (MonitorState.FAILING, 1)
    assert state.last_error_kind is ErrorKind.TIMEOUT
    assert state.next_run_at - await db_now(engine) <= RETRY_DELAY

    await run(engine, check_id, FAIL)  # a confirmation retry
    results = sorted(await rows(engine, CheckResult), key=lambda r: r.checked_at)
    assert [r.is_retry for r in results] == [False, True]
    [rollup] = await rows(engine, CheckStatsHourly)
    assert (rollup.total, rollup.ok) == (1, 0)


async def test_a_blip_opens_no_incident(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    await run(engine, check_id, FAIL)
    await run(engine, check_id, FAIL)
    await run(engine, check_id, OK)

    assert (await state_of(engine, check_id)).state is MonitorState.UP
    assert await rows(engine, Incident) == []


# ---------- Completion: incidents and the outbox ----------


async def go_down(engine: AsyncEngine, check_id: int) -> None:
    for _ in range(3):
        await run(engine, check_id, FAIL)


async def test_third_failure_opens_an_incident_and_queues_down_emails(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice, alert_emails=["a@x.io", "b@x.io"])
    await go_down(engine, check_id)

    results = sorted(await rows(engine, CheckResult), key=lambda r: r.checked_at)
    [incident] = await rows(engine, Incident)
    assert incident.started_at == results[0].checked_at  # backdated to the first failure
    assert incident.detected_at == results[2].checked_at
    assert incident.ended_at is None
    assert (incident.cause_kind, incident.cause_message) == (
        ErrorKind.TIMEOUT,
        "no response within 10 s",
    )
    assert incident.down_alert_queued
    assert not incident.began_in_maintenance

    emails = await rows(engine, Notification)
    assert {(n.kind, n.recipient) for n in emails} == {
        (AlertKind.DOWN, "a@x.io"),
        (AlertKind.DOWN, "b@x.io"),
    }
    assert all(n.incident_id == incident.id for n in emails)

    state = await state_of(engine, check_id)
    assert (state.state, state.consecutive_failures) == (MonitorState.DOWN, 3)
    # Back on the normal schedule, re-anchored after the retries.
    assert state.next_run_at - await db_now(engine) > RETRY_DELAY


async def test_more_failures_while_down_change_nothing(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    await go_down(engine, check_id)
    await run(engine, check_id, FAIL)
    await run(engine, check_id, FAIL)

    assert len(await rows(engine, Incident)) == 1
    assert len(await rows(engine, Notification)) == 1


async def test_grouped_check_alerts_the_groups_recipients(engine: AsyncEngine, alice: int) -> None:
    group = await add_group(engine, alice, alert_emails=["ops@x.io", "oncall@x.io"])
    check_id = await add_check(engine, alice, group_id=group, alert_emails=["own@x.io"])
    await go_down(engine, check_id)

    emails = await rows(engine, Notification)
    assert {n.recipient for n in emails} == {"ops@x.io", "oncall@x.io"}


async def test_recovery_closes_the_incident_and_queues_up_emails(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await go_down(engine, check_id)
    await run(engine, check_id, OK)

    [incident] = await rows(engine, Incident)
    last = max(r.checked_at for r in await rows(engine, CheckResult))
    assert (incident.ended_at, incident.end_reason) == (last, IncidentEndReason.RECOVERED)
    emails = await rows(engine, Notification)
    assert sorted(n.kind for n in emails) == [AlertKind.DOWN, AlertKind.UP]
    state = await state_of(engine, check_id)
    assert (state.state, state.consecutive_failures, state.first_failure_at) == (
        MonitorState.UP,
        0,
        None,
    )


async def test_maintenance_on_the_check_suppresses_the_down_email(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await add_window(engine, alice, check_id=check_id)
    await go_down(engine, check_id)

    [incident] = await rows(engine, Incident)
    assert incident.began_in_maintenance
    assert not incident.down_alert_queued
    assert await rows(engine, Notification) == []


async def test_maintenance_on_the_group_suppresses_the_down_email(
    engine: AsyncEngine, alice: int
) -> None:
    group = await add_group(engine, alice)
    check_id = await add_check(engine, alice, group_id=group)
    await add_window(engine, alice, group_id=group)
    await go_down(engine, check_id)

    assert await rows(engine, Notification) == []


async def test_maintenance_on_another_check_does_not_apply(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    other = await add_check(engine, alice)
    await sql(engine, "UPDATE check_state SET next_run_at = now() + interval '1 h'")
    await add_window(engine, alice, check_id=other)
    await go_down(engine, check_id)

    assert len(await rows(engine, Notification)) == 1


async def test_past_and_future_windows_do_not_apply(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    hour = timedelta(hours=1)
    await add_window(
        engine, alice, check_id=check_id, starts_at=func.now() - 2 * hour, ends_at=func.now() - hour
    )
    await add_window(
        engine, alice, check_id=check_id, starts_at=func.now() + hour, ends_at=func.now() + 2 * hour
    )
    await go_down(engine, check_id)

    assert len(await rows(engine, Notification)) == 1


async def test_recovery_sends_no_up_email_if_down_was_suppressed(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await add_window(engine, alice, check_id=check_id)
    await go_down(engine, check_id)
    await run(engine, check_id, OK)

    [incident] = await rows(engine, Incident)
    assert incident.end_reason is IncidentEndReason.RECOVERED
    assert await rows(engine, Notification) == []


async def test_recovery_during_maintenance_still_sends_up_if_down_was_sent(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await go_down(engine, check_id)
    await add_window(engine, alice, check_id=check_id)
    await run(engine, check_id, OK)

    emails = await rows(engine, Notification)
    assert sorted(n.kind for n in emails) == [AlertKind.DOWN, AlertKind.UP]


# ---------- Fencing and concurrent changes ----------


async def test_result_is_discarded_after_losing_the_lease(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    zombie = await claim_one(engine, check_id, W1)
    await expire_lease(engine, check_id)
    await claim_one(engine, check_id, W2)

    assert await complete(engine, W1, zombie, OK, zombie.claimed_at) is Completion.FENCED
    assert await rows(engine, CheckResult) == []
    assert (await state_of(engine, check_id)).lease_owner == W2


async def test_an_older_lease_of_the_same_worker_is_fenced(engine: AsyncEngine, alice: int) -> None:
    check_id = await add_check(engine, alice)
    stale = await claim_one(engine, check_id, W1)
    await expire_lease(engine, check_id)
    await claim_one(engine, check_id, W1)

    assert await complete(engine, W1, stale, OK, stale.claimed_at) is Completion.FENCED


@pytest.mark.parametrize("change", ["is_paused = true", "deleted_at = now()"])
async def test_result_is_discarded_if_paused_or_deleted_while_running(
    engine: AsyncEngine, alice: int, change: str
) -> None:
    check_id = await add_check(engine, alice)
    c = await claim_one(engine, check_id)
    await sql(engine, f"UPDATE checks SET {change} WHERE id = :id", id=check_id)

    assert await complete(engine, W1, c, FAIL, c.claimed_at) is Completion.DISCARDED
    assert await rows(engine, CheckResult) == []
    state = await state_of(engine, check_id)
    assert (state.state, state.lease_owner, state.lease_until) == (MonitorState.PENDING, None, None)


async def test_completion_waits_for_a_pause_holding_the_state_lock(
    engine: AsyncEngine, alice: int
) -> None:
    """The lock-order rule, sequence (f): whoever locks check_state second sees the other's
    committed result."""
    check_id = await add_check(engine, alice)
    c = await claim_one(engine, check_id)

    async with engine.begin() as api:
        await api.execute(
            select(CheckState.check_id).where(CheckState.check_id == check_id).with_for_update()
        )
        await api.execute(update(Check).where(Check.id == check_id).values(is_paused=True))
        completion = asyncio.create_task(complete(engine, W1, c, OK, c.claimed_at))
        await asyncio.sleep(0.3)
        assert not completion.done()  # blocked on the check_state row lock

    assert await completion is Completion.DISCARDED


# ---------- Realtime ----------


@pytest.fixture
async def listener(settings: Settings) -> AsyncGenerator[asyncio.Queue[dict[str, Any]]]:
    url = str(settings.database_url).replace("+asyncpg", "")
    conn = await cast(Any, asyncpg).connect(url)  # asyncpg ships no type hints
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
        queue.put_nowait(json.loads(payload))

    await conn.add_listener(NOTIFY_CHANNEL, on_notify)
    yield queue
    await conn.close()


async def test_completion_notifies_listeners(
    engine: AsyncEngine, alice: int, listener: asyncio.Queue[dict[str, Any]]
) -> None:
    check_id = await add_check(engine, alice)
    await run(engine, check_id, OK)

    event = await asyncio.wait_for(listener.get(), timeout=2)
    assert event == {"check_id": check_id, "owner_id": alice, "state": "up", "rt_ms": 120}


async def test_discarded_results_notify_nothing(
    engine: AsyncEngine, alice: int, listener: asyncio.Queue[dict[str, Any]]
) -> None:
    check_id = await add_check(engine, alice)
    c = await claim_one(engine, check_id)
    await sql(engine, "UPDATE checks SET is_paused = true WHERE id = :id", id=check_id)
    await complete(engine, W1, c, OK, c.claimed_at)

    await asyncio.sleep(0.2)
    assert listener.empty()


# ---------- Startup spread ----------


async def test_startup_spreads_long_overdue_checks(engine: AsyncEngine, alice: int) -> None:
    overdue = await add_check(engine, alice, interval_s=300)
    await sql(
        engine,
        "UPDATE check_state SET next_run_at = now() - interval '2 hours' WHERE check_id = :id",
        id=overdue,
    )
    recent = await add_check(engine, alice)  # due now, but not overdue by an interval
    paused = await add_check(engine, alice, is_paused=True)
    await sql(
        engine,
        "UPDATE check_state SET next_run_at = now() - interval '2 hours' WHERE check_id = :id",
        id=paused,
    )
    before = {i: (await state_of(engine, i)).next_run_at for i in (recent, paused)}

    assert await spread_overdue_call_times(engine) == 1

    now = await db_now(engine)
    moved = (await state_of(engine, overdue)).next_run_at
    assert now <= moved <= now + timedelta(seconds=60)  # min(interval, 60 s)
    for check_id, next_run in before.items():
        assert (await state_of(engine, check_id)).next_run_at == next_run
