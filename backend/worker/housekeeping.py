"""Housekeeping (`design/architecture.md`): daily `check_results` partitions, retention, and
purging soft-deleted checks. Runs at worker startup, then hourly.

Only one worker housekeeps at a time (a session advisory lock); the others skip the round.
Each step is its own short transaction: partition DDL locks the whole `check_results`
table, so it must not be held while the rest runs.
"""

import logging
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.models import Check, CheckResult, CheckStatsHourly, WorkerHeartbeat

log = logging.getLogger("worker.housekeeping")

LOCK_KEY = 0x5A6B_0001  # advisory lock id, used only by housekeeping
RAW_RETENTION_DAYS = 7
PARTITIONS_AHEAD_DAYS = 7
# A soft-deleted check is hard-deleted once its raw results have aged out too.
PURGE_DELETED_AFTER = timedelta(days=RAW_RETENTION_DAYS + 1)
HEARTBEAT_STALE_AFTER = timedelta(hours=1)
PURGE_BATCH = 500
# Partition DDL waits for every query on check_results and blocks inserts while it waits:
# give up quickly and try again next round instead.
DDL_LOCK_TIMEOUT = "5s"
PARTITION = re.compile(r"check_results_(\d{8})")


@dataclass
class Report:
    created: list[date] = field(default_factory=list[date])
    dropped: list[date] = field(default_factory=list[date])
    failed: list[date] = field(default_factory=list[date])  # retried next round
    default_rows_deleted: int = 0
    rollups_deleted: int = 0
    checks_purged: int = 0
    heartbeats_pruned: int = 0

    def __str__(self) -> str:
        """Only what happened, e.g. 'created 2026-11-06; checks_purged 2'."""
        parts: list[str] = []
        for f in fields(self):
            value: list[date] | int | str = getattr(self, f.name)
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            if value:
                parts.append(f"{f.name} {value}")
        return "; ".join(parts) or "nothing to do"


def partition_name(day: date) -> str:
    return f"check_results_{day:%Y%m%d}"


async def run(engine: AsyncEngine, rollup_retention_days: int) -> Report | None:
    """One round. Returns None if another worker holds the housekeeping lock."""
    async with engine.connect() as conn:
        locked = await conn.scalar(select(func.pg_try_advisory_lock(LOCK_KEY)))
        await conn.commit()
        if not locked:
            return None
        try:
            report = await _housekeep(conn, rollup_retention_days)
        finally:
            await conn.execute(select(func.pg_advisory_unlock(LOCK_KEY)))
            await conn.commit()
    if report != Report():
        log.info("housekeeping: %s", report)
    return report


async def _housekeep(conn: AsyncConnection, rollup_retention_days: int) -> Report:
    report = Report()
    async with conn.begin():
        now: datetime = (await conn.execute(select(func.now()))).scalar_one()
        existing = await daily_partitions(conn)
    today = now.astimezone(UTC).date()

    for day in (today + timedelta(days=n) for n in range(PARTITIONS_AHEAD_DAYS + 1)):
        if day in existing:
            continue
        # Fails if the default partition already holds rows for that day (partitions ran
        # out). The day stays in the default; those rows age out by the DELETE below.
        ddl = (
            f"CREATE TABLE {partition_name(day)} PARTITION OF check_results"
            f" FOR VALUES FROM ('{day} 00:00+00') TO ('{day + timedelta(days=1)} 00:00+00')"
        )
        if await _ddl(conn, ddl, day):
            report.created.append(day)
        else:
            report.failed.append(day)

    oldest_kept = today - timedelta(days=RAW_RETENTION_DAYS)
    for day in sorted(d for d in existing if d < oldest_kept):
        if await _ddl(conn, f"DROP TABLE {existing[day]}", day):
            report.dropped.append(day)
        else:
            report.failed.append(day)

    cutoff = datetime.combine(oldest_kept, time(), UTC)
    async with conn.begin():
        # Only rows outside every remaining partition: the default, or a failed drop.
        report.default_rows_deleted = (
            await conn.execute(delete(CheckResult).where(CheckResult.checked_at < cutoff))
        ).rowcount
        report.rollups_deleted = (
            await conn.execute(
                delete(CheckStatsHourly).where(
                    CheckStatsHourly.bucket < func.now() - timedelta(days=rollup_retention_days)
                )
            )
        ).rowcount
        report.heartbeats_pruned = (
            await conn.execute(
                delete(WorkerHeartbeat).where(
                    WorkerHeartbeat.last_seen_at < func.now() - HEARTBEAT_STALE_AFTER
                )
            )
        ).rowcount

    # Hard delete cascades to everything the check owns, so go in batches.
    while True:
        async with conn.begin():
            batch = (
                select(Check.id)
                .where(Check.deleted_at < func.now() - PURGE_DELETED_AFTER)
                .limit(PURGE_BATCH)
            )
            purged = (await conn.execute(delete(Check).where(Check.id.in_(batch)))).rowcount
        report.checks_purged += purged
        if purged < PURGE_BATCH:
            break
    return report


async def daily_partitions(conn: AsyncConnection) -> dict[date, str]:
    """The daily partitions of check_results, by day (the default partition excluded)."""
    names = await conn.scalars(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = 'check_results'::regclass"
        )
    )
    days: dict[date, str] = {}
    for name in names:
        if match := PARTITION.fullmatch(name):
            days[datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=UTC).date()] = name
    return days


async def _ddl(conn: AsyncConnection, statement: str, day: date) -> bool:
    try:
        async with conn.begin():
            await conn.execute(text(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'"))
            await conn.execute(text(statement))
    except DBAPIError as exc:
        log.error("partition %s: %s", day, str(exc.orig).strip())
        return False
    return True
