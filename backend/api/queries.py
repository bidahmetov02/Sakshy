"""Queries shared by several routes: ownership, the maintenance overlay, status tiers,
chart buckets and pagination cursors. Times come from the database clock."""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import (
    ColumnElement,
    DateTime,
    Exists,
    Integer,
    Numeric,
    ScalarSelect,
    and_,
    exists,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas import Bucket, Stats, Status
from core.models import Check, CheckGroup, CheckResult, CheckStatsHourly, MaintenanceWindow

# ---------- Ownership ----------


def check_not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "check not found")


async def require_check(session: AsyncSession, user_id: int, check_id: int) -> None:
    """404 unless the check is the user's and not deleted."""
    found = (
        await session.execute(
            select(Check.id).where(
                Check.id == check_id, Check.owner_id == user_id, Check.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if found is None:
        raise check_not_found()


def field_error(loc: str | tuple[str | int, ...], msg: str, value: Any) -> RequestValidationError:
    """A 422 shaped like FastAPI's own, pointing at one place in the body."""
    path = (loc,) if isinstance(loc, str) else loc
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", *path), "msg": msg, "input": value}]
    )


async def hold_check(session: AsyncSession, user_id: int, check_id: int, field: str) -> None:
    """422 on `field` unless the check is the user's and not deleted. KEY SHARE keeps it
    from being purged before this transaction commits."""
    found = (
        await session.execute(
            select(Check.id)
            .where(Check.id == check_id, Check.owner_id == user_id, Check.deleted_at.is_(None))
            .with_for_update(key_share=True)
        )
    ).one_or_none()
    if found is None:
        raise field_error(field, "check not found", check_id)


async def hold_group(session: AsyncSession, user_id: int, group_id: int) -> None:
    """422 unless the group is the user's. KEY SHARE keeps it from being deleted before
    this transaction commits."""
    found = (
        await session.execute(
            select(CheckGroup.id)
            .where(CheckGroup.id == group_id, CheckGroup.owner_id == user_id)
            .with_for_update(key_share=True)
        )
    ).one_or_none()
    if found is None:
        raise field_error("group_id", "group not found", group_id)


async def db_now(session: AsyncSession) -> datetime:
    """The database clock (the transaction's start time)."""
    return (await session.execute(select(func.now()))).scalar_one()


# ---------- Maintenance overlay ----------


def _active_window() -> ColumnElement[bool]:
    """A window active now that targets `Check` or its group (as of now). Correlates with
    the enclosing query's `Check`."""
    w = MaintenanceWindow
    return and_(
        w.starts_at <= func.now(),
        w.ends_at > func.now(),
        or_(w.check_id == Check.id, w.group_id == Check.group_id),
    )


def in_maintenance() -> Exists:
    return exists().where(_active_window())


def maintenance_since() -> ScalarSelect[datetime]:
    """Start of the earliest active window, or null when not in maintenance."""
    return select(func.min(MaintenanceWindow.starts_at)).where(_active_window()).scalar_subquery()


# ---------- Status tiers ----------


def counts_toward_status() -> ColumnElement[bool]:
    """Paused checks and checks in maintenance don't count toward a group's or page's status."""
    return and_(Check.is_paused.is_(False), ~in_maintenance())


def tiered_status(counted: int, down: int) -> Status:
    """`failing` isn't down yet: only confirmed outages count."""
    if down == 0:
        return "operational"
    return "major_outage" if down == counted else "partial_outage"


# ---------- Chart buckets ----------

Range = Literal["day", "week", "month"]
RangeQuery = Annotated[Range, Query(alias="range")]

# (slot length in seconds, number of slots), the last slot being the current one
_RANGES: dict[Range, tuple[int, int]] = {
    "day": (300, 288),  # from raw results
    "week": (3600, 168),  # from hourly rollups
    "month": (3600, 720),
}
UPTIME_RANGE: Range = "month"  # what a public page's uptime % covers


def _interval(seconds: int) -> ColumnElement[Any]:
    # Literal SQL (from the constants above, never user input): the slot expression is then
    # textually identical in SELECT and GROUP BY.
    return literal_column(f"interval '{seconds} seconds'")


# Slots are aligned to UTC (the rollups' hours are UTC hours).
_ORIGIN: ColumnElement[datetime] = literal_column(
    "timestamptz '2000-01-01 00:00+00'", DateTime(timezone=True)
)


async def stats(
    session: AsyncSession,
    check_id: int,
    range_: Range,
    *,
    uptime: bool = True,
    response_times: bool = True,
) -> Stats:
    """Every slot of the range, empty ones as gaps. Retries are not counted, so a burst of
    them doesn't skew the numbers. Fields left out by the flags are left unset."""
    seconds, count = _RANGES[range_]
    step = _interval(seconds)
    last = func.date_bin(step, func.now(), _ORIGIN)
    first = last - _interval(seconds * (count - 1))
    series = func.generate_series(first, last, step).table_valued("t").render_derived()

    if range_ == "day":
        r = CheckResult
        slot = func.date_bin(step, r.checked_at, _ORIGIN)
        agg = (
            select(
                slot.label("t"),
                func.count().label("total"),
                func.count().filter(r.ok).label("ok"),
                func.round(func.avg(r.response_ms)).cast(Integer).label("avg_ms"),
                func.max(r.response_ms).label("max_ms"),
            )
            .where(r.check_id == check_id, r.is_retry.is_(False), r.checked_at >= first)
            .group_by(slot)
            .subquery()
        )
    else:
        h = CheckStatsHourly
        avg = h.rt_sum_ms.cast(Numeric) / func.nullif(h.rt_count, 0)
        agg = (
            select(
                h.bucket.label("t"),
                h.total.label("total"),
                h.ok.label("ok"),
                func.round(avg).cast(Integer).label("avg_ms"),
                h.rt_max_ms.label("max_ms"),
            )
            .where(h.check_id == check_id, h.bucket >= first)
            .subquery()
        )
    rows = (
        await session.execute(
            select(series.c.t, agg.c.total, agg.c.ok, agg.c.avg_ms, agg.c.max_ms)
            .outerjoin(agg, agg.c.t == series.c.t)
            .order_by(series.c.t)
        )
    ).all()

    buckets: list[Bucket] = []
    for t, total, ok, avg_ms, max_ms in rows:
        fields: dict[str, Any] = {"t": t}
        if uptime:
            fields |= {"total": total, "ok": ok}
        if response_times:
            fields |= {"avg_ms": avg_ms, "max_ms": max_ms}
        buckets.append(Bucket(**fields))
    if not uptime:
        return Stats(buckets=buckets)
    total = sum(row.total or 0 for row in rows)
    ok = sum(row.ok or 0 for row in rows)
    return Stats(uptime_pct=100 * ok / total if total else None, buckets=buckets)


async def uptime_pct(session: AsyncSession, check_ids: list[int]) -> dict[int, float]:
    """Uptime % over `UPTIME_RANGE` from the rollups, for checks that have runs in it."""
    seconds, count = _RANGES[UPTIME_RANGE]
    first = func.date_bin(_interval(seconds), func.now(), _ORIGIN) - _interval(
        seconds * (count - 1)
    )
    h = CheckStatsHourly
    rows = await session.execute(
        select(h.check_id, func.sum(h.ok), func.sum(h.total))
        .where(h.check_id.in_(check_ids), h.bucket >= first)
        .group_by(h.check_id)
    )
    return {check_id: 100 * ok / total for check_id, ok, total in rows if total}


# ---------- Pagination ----------

Limit = Annotated[int, Query(ge=1, le=200)]


def encode_cursor(at: datetime, id_: int = 0) -> str:
    """Opaque to clients: the sort key of the last item returned."""
    raw = json.dumps([at.isoformat(), id_]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        at, id_ = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        at = datetime.fromisoformat(at)
        if at.tzinfo is None:
            raise ValueError("naive timestamp")
        return at, int(id_)
    except binascii.Error, ValueError, TypeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor") from None
