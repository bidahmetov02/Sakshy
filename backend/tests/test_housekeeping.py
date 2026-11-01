"""Housekeeping against real Postgres: partitions, retention, purge, the advisory lock."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import (
    Check,
    CheckResult,
    CheckState,
    CheckStatsHourly,
    Incident,
    WorkerHeartbeat,
)
from tests.factories import add_check, add_incident, add_user, rows
from worker import housekeeping
from worker.housekeeping import (
    LOCK_KEY,
    PARTITIONS_AHEAD_DAYS,
    RAW_RETENTION_DAYS,
    Report,
    daily_partitions,
    partition_name,
)

RETENTION = 90


async def housekeep(engine: AsyncEngine) -> Report:
    report = await housekeeping.run(engine, RETENTION)
    assert report is not None
    return report


async def sql(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def db_today(engine: AsyncEngine) -> date:
    async with engine.connect() as conn:
        now: datetime = (await conn.execute(select(func.now()))).scalar_one()
    return now.astimezone(UTC).date()


async def partitions(engine: AsyncEngine) -> set[date]:
    async with engine.connect() as conn:
        return set(await daily_partitions(conn))


async def create_partition(engine: AsyncEngine, day: date) -> None:
    await sql(
        engine,
        f"CREATE TABLE {partition_name(day)} PARTITION OF check_results"
        f" FOR VALUES FROM ('{day} 00:00+00') TO ('{day + timedelta(days=1)} 00:00+00')",
    )


async def add_result(engine: AsyncEngine, checked_at: datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(CheckResult).values(check_id=1, checked_at=checked_at, ok=True))


async def placed(engine: AsyncEngine) -> list[tuple[str, datetime]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tableoid::regclass::text, checked_at FROM check_results ORDER BY 2")
        )
        return [(r[0], r[1]) for r in result]


def noon(day: date) -> datetime:
    return datetime.combine(day, time(12), UTC)


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=n) for n in range(count)]


# ---------- Partitions ----------


async def test_creates_partitions_from_today_for_a_week_ahead(engine: AsyncEngine) -> None:
    today = await db_today(engine)
    report = await housekeep(engine)

    week = days(today, PARTITIONS_AHEAD_DAYS + 1)
    assert report.created == week
    assert await partitions(engine) == set(week)
    assert await housekeep(engine) == Report()  # nothing left to do


async def test_results_land_in_their_days_partition(engine: AsyncEngine) -> None:
    today = await db_today(engine)
    await housekeep(engine)
    await add_result(engine, noon(today))
    await add_result(engine, noon(today + timedelta(days=PARTITIONS_AHEAD_DAYS)))

    assert [name for name, _ in await placed(engine)] == [
        partition_name(today),
        partition_name(today + timedelta(days=PARTITIONS_AHEAD_DAYS)),
    ]


async def test_drops_partitions_past_the_retention(engine: AsyncEngine) -> None:
    oldest_kept = await db_today(engine) - timedelta(days=RAW_RETENTION_DAYS)
    expired = oldest_kept - timedelta(days=1)
    for day in (expired - timedelta(days=1), expired, oldest_kept):
        await create_partition(engine, day)
    await add_result(engine, noon(expired))
    await add_result(engine, noon(oldest_kept))

    report = await housekeep(engine)

    assert report.dropped == [expired - timedelta(days=1), expired]
    assert oldest_kept in await partitions(engine)
    assert [at for _, at in await placed(engine)] == [noon(oldest_kept)]


async def test_deletes_expired_rows_from_the_default_partition(engine: AsyncEngine) -> None:
    oldest_kept = await db_today(engine) - timedelta(days=RAW_RETENTION_DAYS)
    expired_at = datetime.combine(oldest_kept, time(), UTC) - timedelta(microseconds=1)
    await add_result(engine, expired_at)
    await add_result(engine, noon(oldest_kept))  # no partition: lands in the default

    report = await housekeep(engine)

    assert report.default_rows_deleted == 1
    assert await placed(engine) == [("check_results_default", noon(oldest_kept))]


async def test_a_day_already_in_the_default_partition_stays_there(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """If partitions ran out, that day can't get its own table any more. The rest are still
    created, and the day's rows age out of the default like any others."""
    today = await db_today(engine)
    stuck = today + timedelta(days=3)
    await add_result(engine, noon(stuck))

    with caplog.at_level(logging.ERROR, logger="worker.housekeeping"):
        report = await housekeep(engine)

    assert report.failed == [stuck]
    assert report.created == [d for d in days(today, PARTITIONS_AHEAD_DAYS + 1) if d != stuck]
    assert f"partition {stuck}" in caplog.text
    assert await placed(engine) == [("check_results_default", noon(stuck))]
    # Inserts for that day keep working, into the default.
    await add_result(engine, noon(stuck) + timedelta(hours=1))
    assert len(await placed(engine)) == 2


# ---------- Retention and purge ----------


async def test_prunes_old_rollups_and_stale_heartbeats(engine: AsyncEngine) -> None:
    check_id = await add_check(engine, await add_user(engine))
    async with engine.begin() as conn:
        for age in (RETENTION + 1, RETENTION - 1):
            await conn.execute(
                insert(CheckStatsHourly).values(
                    check_id=check_id,
                    bucket=func.date_trunc("hour", func.now() - timedelta(days=age)),
                    total=1,
                    ok=1,
                    rt_count=0,
                    rt_sum_ms=0,
                )
            )
        for worker_id, seen in (("dead", timedelta(hours=2)), ("alive", timedelta(seconds=5))):
            await conn.execute(
                insert(WorkerHeartbeat).values(
                    worker_id=worker_id, started_at=func.now(), last_seen_at=func.now() - seen
                )
            )

    report = await housekeep(engine)

    assert (report.rollups_deleted, report.heartbeats_pruned) == (1, 1)
    assert len(await rows(engine, CheckStatsHourly)) == 1
    assert [h.worker_id for h in await rows(engine, WorkerHeartbeat)] == ["alive"]


async def soft_delete(engine: AsyncEngine, check_id: int, days_ago: int) -> None:
    await sql(
        engine,
        "UPDATE checks SET deleted_at = now() - make_interval(days => :d) WHERE id = :id",
        d=days_ago,
        id=check_id,
    )


async def test_purges_checks_deleted_more_than_eight_days_ago(engine: AsyncEngine) -> None:
    alice = await add_user(engine)
    old, recent, live = [await add_check(engine, alice) for _ in range(3)]
    await add_incident(engine, old)
    await soft_delete(engine, old, 9)
    await soft_delete(engine, recent, 7)

    report = await housekeep(engine)

    assert report.checks_purged == 1
    assert sorted(c.id for c in await rows(engine, Check)) == [recent, live]
    assert sorted(s.check_id for s in await rows(engine, CheckState)) == [recent, live]
    assert await rows(engine, Incident) == []


async def test_purges_in_batches(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(housekeeping, "PURGE_BATCH", 2)
    alice = await add_user(engine)
    for _ in range(5):
        await soft_delete(engine, await add_check(engine, alice), 9)

    assert (await housekeep(engine)).checks_purged == 5
    assert await rows(engine, Check) == []


# ---------- One worker at a time ----------


async def test_skips_the_round_while_another_worker_holds_the_lock(engine: AsyncEngine) -> None:
    async with engine.connect() as other:
        assert await other.scalar(select(func.pg_try_advisory_lock(LOCK_KEY)))
        assert await housekeeping.run(engine, RETENTION) is None
        await other.scalar(select(func.pg_advisory_unlock(LOCK_KEY)))

    assert await partitions(engine) == set()
    assert await housekeeping.run(engine, RETENTION) is not None


async def test_releases_the_lock_after_a_round(engine: AsyncEngine) -> None:
    # Checked out first, so housekeeping runs on another session (the lock is re-entrant).
    async with engine.connect() as other:
        await housekeep(engine)
        assert await other.scalar(select(func.pg_try_advisory_lock(LOCK_KEY)))
        await other.scalar(select(func.pg_advisory_unlock(LOCK_KEY)))
