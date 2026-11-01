"""Maintenance windows: one-off, on one check or one group. A window suppresses DOWN
emails only; when it ends with the site still down, the sweep queues the DOWN email.

Times are checked against the database clock, and a time in the past means "now": a
`starts_at` on create or before the window starts, an `ends_at` while it is active (ending
it early). So a browser whose clock runs a little behind still gets what it asked for.
Another user's window, or one on a deleted check, is a 404.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import ColumnElement, Select, case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Session, UserId
from api.queries import db_now, field_error, hold_check, hold_group
from api.schemas import MaintenanceIn, MaintenanceOut, MaintenancePatch, WindowStatus
from core.models import Check, MaintenanceWindow

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

w = MaintenanceWindow


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "maintenance window not found")


def _status() -> ColumnElement[str]:
    return case(
        (w.ends_at <= func.now(), "past"), (w.starts_at > func.now(), "upcoming"), else_="active"
    )


def _visible[*Ts](stmt: Select[*Ts], user_id: int) -> Select[*Ts]:
    """The user's windows, except those on deleted checks."""
    return (
        stmt.outerjoin(Check, Check.id == w.check_id)
        .where(w.owner_id == user_id)
        .where(or_(w.check_id.is_(None), Check.deleted_at.is_(None)))
    )


def _out(window: MaintenanceWindow, status_: str) -> MaintenanceOut:
    return MaintenanceOut(
        id=window.id,
        check_id=window.check_id,
        group_id=window.group_id,
        starts_at=window.starts_at,
        ends_at=window.ends_at,
        reason=window.reason,
        created_at=window.created_at,
        status=status_,  # pyright: ignore[reportArgumentType]  # one of the case() values
    )


async def _get(session: AsyncSession, user_id: int, window_id: int) -> MaintenanceOut:
    stmt = _visible(select(w, _status()), user_id).where(w.id == window_id)
    row = (await session.execute(stmt.execution_options(populate_existing=True))).one_or_none()
    if row is None:
        raise _not_found()
    return _out(*row)


async def _lock(session: AsyncSession, user_id: int, window_id: int) -> MaintenanceWindow:
    stmt = _visible(select(w), user_id).where(w.id == window_id).with_for_update(of=w)
    window = (await session.scalars(stmt)).one_or_none()
    if window is None:
        raise _not_found()
    return window


@router.get("")
async def list_windows(
    user_id: UserId,
    session: Session,
    status_: Annotated[WindowStatus | None, Query(alias="status")] = None,
) -> list[MaintenanceOut]:
    """Newest first."""
    stmt = _visible(select(w, _status()), user_id).order_by(w.starts_at.desc(), w.id.desc())
    if status_ is not None:
        stmt = stmt.where(_status() == status_)
    return [_out(*row) for row in await session.execute(stmt)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_window(body: MaintenanceIn, user_id: UserId, session: Session) -> MaintenanceOut:
    if body.check_id is not None:
        await hold_check(session, user_id, body.check_id, "check_id")
    if body.group_id is not None:
        await hold_group(session, user_id, body.group_id)
    starts_at = max(body.starts_at, await db_now(session))
    if body.ends_at <= starts_at:
        raise field_error("ends_at", "must be in the future and after starts_at", body.ends_at)
    window = MaintenanceWindow(owner_id=user_id, **body.model_dump() | {"starts_at": starts_at})
    session.add(window)
    await session.commit()
    return await _get(session, user_id, window.id)


@router.patch(
    "/{window_id}",
    responses={409: {"description": "the window has ended, or has started (for starts_at)"}},
)
async def update_window(
    window_id: int, body: MaintenancePatch, user_id: UserId, session: Session
) -> MaintenanceOut:
    """End early: set `ends_at` to now (or any past time)."""
    window = await _lock(session, user_id, window_id)
    now = await db_now(session)
    if window.ends_at <= now:
        raise HTTPException(status.HTTP_409_CONFLICT, "window has ended")
    changes = body.model_dump(exclude_unset=True)
    starts_at = changes.get("starts_at", window.starts_at)
    if starts_at != window.starts_at:
        if window.starts_at <= now:
            raise HTTPException(status.HTTP_409_CONFLICT, "window has started")
        starts_at = max(starts_at, now)
    ends_at = max(changes.get("ends_at", window.ends_at), now)
    if ends_at <= starts_at:
        raise field_error("ends_at", "must be after starts_at", changes.get("ends_at"))
    window.starts_at, window.ends_at = starts_at, ends_at
    if "reason" in changes:
        window.reason = changes["reason"]
    await session.commit()
    return await _get(session, user_id, window_id)


@router.delete("/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_window(window_id: int, user_id: UserId, session: Session) -> None:
    window = await _lock(session, user_id, window_id)
    await session.execute(delete(w).where(w.id == window.id))
    await session.commit()
