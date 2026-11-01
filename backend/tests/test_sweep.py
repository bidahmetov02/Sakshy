"""The maintenance sweep against real Postgres (design/sequences.md (b))."""

import asyncio

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import AlertKind, Check, CheckState, Incident, MaintenanceWindow, Notification
from tests.factories import add_check, add_group, add_incident, add_user, add_window, rows
from tests.test_runs import FAIL, OK, go_down, run
from worker.sweep import sweep

ENDED = {
    "starts_at": func.now() - text("interval '1 hour'"),
    "ends_at": func.now() - text("interval '1 second'"),
}


@pytest.fixture
async def alice(engine: AsyncEngine) -> int:
    return await add_user(engine)


async def suppressed_incident(engine: AsyncEngine, check_id: int) -> int:
    """An open incident whose DOWN email a maintenance window held back."""
    return await add_incident(engine, check_id, began_in_maintenance=True, down_alert_queued=False)


async def emails(engine: AsyncEngine) -> set[tuple[AlertKind, str]]:
    return {(n.kind, n.recipient) for n in await rows(engine, Notification)}


async def test_queues_the_down_email_once_the_window_has_ended(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice, alert_emails=["a@x.io", "b@x.io"])
    await add_window(engine, alice, check_id=check_id, **ENDED)
    await suppressed_incident(engine, check_id)

    assert await sweep(engine) == 1
    assert await emails(engine) == {(AlertKind.DOWN, "a@x.io"), (AlertKind.DOWN, "b@x.io")}
    [incident] = await rows(engine, Incident)
    assert incident.down_alert_queued

    assert await sweep(engine) == 0  # once per incident
    assert len(await rows(engine, Notification)) == 2


@pytest.mark.parametrize("target", ["check", "group"])
async def test_waits_while_a_window_still_applies(
    engine: AsyncEngine, alice: int, target: str
) -> None:
    group = await add_group(engine, alice)
    check_id = await add_check(engine, alice, group_id=group)
    await add_window(
        engine, alice, **({"check_id": check_id} if target == "check" else {"group_id": group})
    )
    await suppressed_incident(engine, check_id)

    assert await sweep(engine) == 0
    assert await rows(engine, Notification) == []


async def test_a_grouped_check_alerts_the_group(engine: AsyncEngine, alice: int) -> None:
    group = await add_group(engine, alice, alert_emails=["ops@x.io"])
    check_id = await add_check(engine, alice, group_id=group, alert_emails=["own@x.io"])
    await add_window(engine, alice, group_id=group, **ENDED)
    await suppressed_incident(engine, check_id)

    await sweep(engine)
    assert await emails(engine) == {(AlertKind.DOWN, "ops@x.io")}


async def test_leaving_a_group_under_maintenance_releases_the_alert(
    engine: AsyncEngine, alice: int
) -> None:
    """Group windows follow the group's current members (state-machine.md)."""
    group = await add_group(engine, alice)
    check_id = await add_check(engine, alice, group_id=group, alert_emails=["own@x.io"])
    await add_window(engine, alice, group_id=group)
    await suppressed_incident(engine, check_id)
    assert await sweep(engine) == 0

    async with engine.begin() as conn:
        await conn.execute(update(Check).where(Check.id == check_id).values(group_id=None))
    assert await sweep(engine) == 1
    assert await emails(engine) == {(AlertKind.DOWN, "own@x.io")}


async def test_ignores_alerted_and_closed_incidents(engine: AsyncEngine, alice: int) -> None:
    alerted = await add_check(engine, alice)
    await add_incident(engine, alerted, down_alert_queued=True)
    closed = await add_check(engine, alice)
    await add_incident(
        engine, closed, down_alert_queued=False, ended_at=func.now(), end_reason="recovered"
    )

    assert await sweep(engine) == 0


async def test_skips_a_check_being_completed_until_the_next_round(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await suppressed_incident(engine, check_id)

    async with engine.begin() as completion:
        await completion.execute(
            select(CheckState.check_id).where(CheckState.check_id == check_id).with_for_update()
        )
        # SKIP LOCKED: the sweep neither waits nor queues anything.
        assert await asyncio.wait_for(sweep(engine), timeout=2) == 0

    assert await sweep(engine) == 1


# ---------- With real completions ----------


async def end_windows(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(update(MaintenanceWindow).values(ends_at=func.now()))


async def test_window_ends_while_down_then_recovery_sends_up(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice, alert_emails=["a@x.io"])
    await add_window(engine, alice, check_id=check_id)
    await go_down(engine, check_id)
    await run(engine, check_id, FAIL)
    assert await rows(engine, Notification) == []

    await end_windows(engine)
    assert await sweep(engine) == 1
    await run(engine, check_id, OK)

    assert await emails(engine) == {(AlertKind.DOWN, "a@x.io"), (AlertKind.UP, "a@x.io")}


async def test_recovery_before_the_window_ends_sends_nothing(
    engine: AsyncEngine, alice: int
) -> None:
    check_id = await add_check(engine, alice)
    await add_window(engine, alice, check_id=check_id)
    await go_down(engine, check_id)
    await run(engine, check_id, OK)
    await end_windows(engine)

    assert await sweep(engine) == 0
    assert await rows(engine, Notification) == []
