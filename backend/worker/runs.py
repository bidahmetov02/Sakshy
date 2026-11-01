"""Claiming due checks and completing their runs (`design/sequences.md` (a) and (d)).

Every time comparison uses the database clock (`now()`), never the worker's.
"""

import enum
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from core.models import (
    AlertKind,
    Check,
    CheckGroup,
    CheckResult,
    CheckState,
    CheckStatsHourly,
    Incident,
    IncidentEndReason,
    MaintenanceWindow,
)
from worker import state_machine as sm
from worker.outbox import alert_recipients, enqueue
from worker.probe import ProbeResult

log = logging.getLogger("worker.runs")

LEASE_MARGIN_S = 5  # the lease outlives the probe's hard timeout by this much
MAX_STARTUP_SPREAD_S = 60
NOTIFY_CHANNEL = "check_events"


def _seconds(value: Any) -> ColumnElement[Any]:
    return func.make_interval(0, 0, 0, 0, 0, 0, value)


@dataclass(frozen=True)
class Claim:
    check_id: int
    url: str
    timeout_s: float
    expected_status: int
    expected_body: str | None
    lease_until: datetime  # fencing token: completion must still hold exactly this lease
    claimed_at: datetime  # database clock


async def claim(engine: AsyncEngine, worker_id: str, limit: int) -> list[Claim]:
    """Lease up to `limit` due checks. SKIP LOCKED: concurrent workers get disjoint rows."""
    s, c = aliased(CheckState), aliased(Check)
    due = (
        select(s.check_id)
        .join(c, c.id == s.check_id)
        .where(
            s.next_run_at <= func.now(),
            or_(s.lease_until.is_(None), s.lease_until < func.now()),
            c.is_paused.is_(False),
            c.deleted_at.is_(None),
        )
        .order_by(s.next_run_at)
        .limit(limit)
        .with_for_update(of=s, skip_locked=True)
    )
    stmt = (
        update(CheckState)
        .where(CheckState.check_id == Check.id, CheckState.check_id.in_(due))
        .values(
            lease_owner=worker_id,
            lease_until=func.now() + _seconds(Check.timeout_s + LEASE_MARGIN_S),
        )
        .returning(
            CheckState.check_id,
            Check.url,
            Check.timeout_s,
            Check.expected_status,
            Check.expected_body,
            CheckState.lease_until,
            func.now(),
        )
    )
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        Claim(
            check_id=r[0],
            url=r[1],
            timeout_s=float(r[2]),
            expected_status=r[3],
            expected_body=r[4],
            lease_until=cast(datetime, r[5]),  # just set, never null
            claimed_at=r[6],
        )
        for r in rows
    ]


async def spread_overdue_call_times(engine: AsyncEngine) -> int:
    """At startup, spread checks overdue by more than an interval (the monitor was down)
    over the next min(interval, 60 s), so they don't all fire in the first second."""
    stmt = (
        update(CheckState)
        .where(
            CheckState.check_id == Check.id,
            CheckState.next_run_at < func.now() - _seconds(Check.interval_s),
            or_(CheckState.lease_until.is_(None), CheckState.lease_until < func.now()),
            Check.is_paused.is_(False),
            Check.deleted_at.is_(None),
        )
        .values(
            next_run_at=func.now()
            + func.random() * _seconds(func.least(Check.interval_s, MAX_STARTUP_SPREAD_S))
        )
    )
    async with engine.begin() as conn:
        return (await conn.execute(stmt)).rowcount


class Completion(enum.Enum):
    RECORDED = "recorded"
    FENCED = "fenced"  # lease lost to another worker: result discarded
    DISCARDED = "discarded"  # paused or deleted while running: lease cleared, result discarded


async def complete(
    engine: AsyncEngine, worker_id: str, claim: Claim, result: ProbeResult, checked_at: datetime
) -> Completion:
    """Record one run in a single transaction: result, rollup, state, incident, outbox, notify.

    Locks the check's `check_state` row first (the lock-order rule), so pause/delete in the
    API and the maintenance sweep always see this run's committed effects, or vice versa.
    """
    async with engine.begin() as conn:
        locked = (
            await conn.execute(
                select(
                    CheckState.next_run_at,
                    CheckState.state,
                    CheckState.consecutive_failures,
                    CheckState.first_failure_at,
                )
                .where(
                    CheckState.check_id == claim.check_id,
                    CheckState.lease_owner == worker_id,
                    CheckState.lease_until == claim.lease_until,
                )
                .with_for_update()
            )
        ).one_or_none()
        if locked is None:
            return Completion.FENCED
        scheduled_at, state, failures, first_failure_at = locked

        # Read the config in a separate statement, after taking the lock. In a join, a
        # statement that waited for the lock re-checks only the locked row and would still
        # see the config from before a pause or delete committed by whoever held it.
        owner_id, group_id, interval_s, is_paused, deleted_at, recipients, now = (
            await conn.execute(
                select(
                    Check.owner_id,
                    Check.group_id,
                    Check.interval_s,
                    Check.is_paused,
                    Check.deleted_at,
                    alert_recipients(),
                    func.now(),
                )
                .outerjoin(CheckGroup, CheckGroup.id == Check.group_id)
                .where(Check.id == claim.check_id)
            )
        ).one()
        if is_paused or deleted_at is not None:
            await conn.execute(
                update(CheckState)
                .where(CheckState.check_id == claim.check_id)
                .values(lease_owner=None, lease_until=None)
            )
            return Completion.DISCARDED

        current = sm.Streak(state, failures, first_failure_at)
        was_retry = sm.is_retry(current)
        transition = sm.step(current, result.ok, checked_at)

        await conn.execute(
            insert(CheckResult).values(
                check_id=claim.check_id,
                checked_at=checked_at,
                ok=result.ok,
                response_ms=result.response_ms,
                status_code=result.status_code,
                error_kind=result.error_kind,
                error_message=result.error_message,
                is_retry=was_retry,
            )
        )
        if not was_retry:
            await _add_to_rollup(conn, claim.check_id, checked_at, result)

        match transition.event:
            case sm.Event.INCIDENT_OPENED:
                await _open_incident(
                    conn,
                    claim.check_id,
                    group_id,
                    transition.streak,
                    checked_at,
                    result,
                    recipients,
                )
            case sm.Event.INCIDENT_RECOVERED:
                await _close_incident(conn, claim.check_id, checked_at, recipients)
            case None:
                pass

        new = transition.streak
        interval = timedelta(seconds=interval_s)
        await conn.execute(
            update(CheckState)
            .where(CheckState.check_id == claim.check_id)
            .values(
                state=new.state,
                consecutive_failures=new.consecutive_failures,
                first_failure_at=new.first_failure_at,
                state_changed_at=now if new.state is not state else CheckState.state_changed_at,
                last_checked_at=checked_at,
                last_ok=result.ok,
                last_status_code=result.status_code,
                last_response_ms=result.response_ms,
                last_error_kind=result.error_kind,
                last_error_message=result.error_message,
                next_run_at=sm.next_run_at(
                    scheduled_at, interval, now, retry=transition.retry, was_retry=was_retry
                ),
                lease_owner=None,
                lease_until=None,
            )
        )
        # Delivered to listeners only if this transaction commits.
        payload = {
            "check_id": claim.check_id,
            "owner_id": owner_id,
            "state": new.state.value,
            "rt_ms": result.response_ms,
        }
        await conn.execute(select(func.pg_notify(NOTIFY_CHANNEL, json.dumps(payload))))
    return Completion.RECORDED


async def _add_to_rollup(
    conn: AsyncConnection, check_id: int, checked_at: datetime, result: ProbeResult
) -> None:
    rt = result.response_ms
    stmt = pg_insert(CheckStatsHourly).values(
        check_id=check_id,
        bucket=checked_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0),
        total=1,
        ok=int(result.ok),
        rt_count=int(rt is not None),
        rt_sum_ms=rt or 0,
        rt_min_ms=rt,
        rt_max_ms=rt,
    )
    h, new = CheckStatsHourly, stmt.excluded
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[h.check_id, h.bucket],
            set_={
                h.total: h.total + 1,
                h.ok: h.ok + new.ok,
                h.rt_count: h.rt_count + new.rt_count,
                h.rt_sum_ms: h.rt_sum_ms + new.rt_sum_ms,
                h.rt_min_ms: func.least(h.rt_min_ms, new.rt_min_ms),
                h.rt_max_ms: func.greatest(h.rt_max_ms, new.rt_max_ms),
            },
        )
    )


async def _open_incident(
    conn: AsyncConnection,
    check_id: int,
    group_id: int | None,
    streak: sm.Streak,
    detected_at: datetime,
    result: ProbeResult,
    recipients: list[str],
) -> None:
    """DOWN: open the incident, and queue the DOWN email unless in maintenance (then the
    sweep queues it when the window ends)."""
    w = MaintenanceWindow
    targets = w.check_id == check_id
    if group_id is not None:
        targets = or_(targets, w.group_id == group_id)
    in_maintenance = (
        await conn.execute(
            select(exists().where(w.starts_at <= func.now(), w.ends_at > func.now(), targets))
        )
    ).scalar_one()
    incident_id = (
        await conn.execute(
            insert(Incident)
            .values(
                check_id=check_id,
                started_at=streak.first_failure_at,
                detected_at=detected_at,
                cause_kind=result.error_kind,
                cause_message=result.error_message,
                began_in_maintenance=in_maintenance,
                down_alert_queued=not in_maintenance,
            )
            .returning(Incident.id)
        )
    ).scalar_one()
    if not in_maintenance:
        await enqueue(conn, incident_id, AlertKind.DOWN, recipients)


async def _close_incident(
    conn: AsyncConnection, check_id: int, ended_at: datetime, recipients: list[str]
) -> None:
    """UP: close the open incident; queue the UP email only if a DOWN email was queued."""
    closed = (
        await conn.execute(
            update(Incident)
            .where(Incident.check_id == check_id, Incident.ended_at.is_(None))
            .values(ended_at=ended_at, end_reason=IncidentEndReason.RECOVERED)
            .returning(Incident.id, Incident.down_alert_queued)
        )
    ).one_or_none()
    if closed is None:
        log.warning("check %d recovered with no open incident", check_id)
        return
    incident_id, down_alert_queued = closed
    if down_alert_queued:
        await enqueue(conn, incident_id, AlertKind.UP, recipients)
