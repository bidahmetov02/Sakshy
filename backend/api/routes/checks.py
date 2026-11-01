"""Checks: CRUD, pause, resume, run now.

Another user's check, or a deleted one, is a 404. Every transaction that changes a check's
runtime state locks its `check_state` row first, then reads the config in a separate
statement (the lock-order rule, `design/architecture.md`).
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Session, UserId
from api.schemas import CheckIn, CheckOut, CheckPatch
from core.events import notify_check
from core.models import (
    Check,
    CheckGroup,
    CheckState,
    Incident,
    IncidentEndReason,
    MonitorState,
)

router = APIRouter(prefix="/checks", tags=["checks"])

_lease_free = or_(CheckState.lease_until.is_(None), CheckState.lease_until < func.now())


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "check not found")


def _snapshot(user_id: int) -> Select[Check, CheckState, list[str], datetime]:
    """Columns from the outer joins (group emails, open incident start) can be null."""
    return (
        select(Check, CheckState, CheckGroup.alert_emails, Incident.started_at)
        .join(CheckState, CheckState.check_id == Check.id)
        .outerjoin(CheckGroup, CheckGroup.id == Check.group_id)
        .outerjoin(Incident, and_(Incident.check_id == Check.id, Incident.ended_at.is_(None)))
        .where(Check.owner_id == user_id, Check.deleted_at.is_(None))
    )


def _out(
    check: Check, state: CheckState, group_emails: list[str] | None, incident_started_at: Any
) -> CheckOut:
    return CheckOut(
        id=check.id,
        name=check.name,
        url=check.url,
        interval_s=check.interval_s,
        timeout_s=float(check.timeout_s),
        expected_status=check.expected_status,
        expected_body=check.expected_body,
        group_id=check.group_id,
        alert_emails=check.alert_emails,
        effective_alert_emails=group_emails or check.alert_emails,
        is_paused=check.is_paused,
        created_at=check.created_at,
        updated_at=check.updated_at,
        state=state.state,
        state_changed_at=state.state_changed_at,
        next_run_at=state.next_run_at,
        last_checked_at=state.last_checked_at,
        last_ok=state.last_ok,
        last_status_code=state.last_status_code,
        last_response_ms=state.last_response_ms,
        last_error_kind=state.last_error_kind,
        last_error_message=state.last_error_message,
        incident_started_at=incident_started_at,
    )


async def _get(session: AsyncSession, user_id: int, check_id: int) -> CheckOut:
    stmt = _snapshot(user_id).where(Check.id == check_id)
    row = (await session.execute(stmt.execution_options(populate_existing=True))).one_or_none()
    if row is None:
        raise _not_found()
    return _out(*row)


async def _lock(session: AsyncSession, user_id: int, check_id: int) -> tuple[Check, CheckState]:
    """Lock the check's `check_state` row, then read its config with a new statement, which
    sees a pause or delete committed while this one waited for the lock."""
    state = (
        await session.scalars(
            select(CheckState)
            .join(Check, Check.id == CheckState.check_id)
            .where(CheckState.check_id == check_id, Check.owner_id == user_id)
            .with_for_update(of=CheckState)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if state is None:
        raise _not_found()
    check = (
        await session.scalars(
            select(Check)
            .where(Check.id == check_id, Check.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if check is None:
        raise _not_found()
    return check, state


async def _hold_group(session: AsyncSession, user_id: int, group_id: int) -> None:
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
        error = {"type": "value_error", "loc": ("body", "group_id"), "msg": "group not found"}
        raise RequestValidationError([error | {"input": group_id}])


def _config(body: CheckIn) -> dict[str, Any]:
    return body.model_dump() | {"timeout_s": Decimal(str(body.timeout_s))}


async def _reset(
    session: AsyncSession, check: Check, state: CheckState, reason: IncidentEndReason
) -> None:
    """Pause and delete: back to pending, and end an open incident with no UP email."""
    await session.execute(
        update(CheckState)
        .where(CheckState.check_id == check.id)
        .values(
            state=MonitorState.PENDING,
            consecutive_failures=0,
            first_failure_at=None,
            state_changed_at=func.now(),
        )
    )
    await session.execute(
        update(Incident)
        .where(Incident.check_id == check.id, Incident.ended_at.is_(None))
        .values(ended_at=func.now(), end_reason=reason)
    )
    await session.execute(
        notify_check(check.id, check.owner_id, MonitorState.PENDING, state.last_response_ms)
    )


@router.get("")
async def list_checks(
    user_id: UserId, session: Session, group_id: int | None = None
) -> list[CheckOut]:
    """The dashboard snapshot: every check with its live state."""
    stmt = _snapshot(user_id).order_by(Check.id)
    if group_id is not None:
        stmt = stmt.where(Check.group_id == group_id)
    return [_out(*row) for row in await session.execute(stmt)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_check(body: CheckIn, user_id: UserId, session: Session) -> CheckOut:
    if body.group_id is not None:
        await _hold_group(session, user_id, body.group_id)
    check = Check(owner_id=user_id, **_config(body))
    session.add(check)
    await session.flush()
    session.add(CheckState(check_id=check.id))  # due now: the first run happens right away
    await session.commit()
    return await _get(session, user_id, check.id)


@router.get("/{check_id}")
async def get_check(check_id: int, user_id: UserId, session: Session) -> CheckOut:
    return await _get(session, user_id, check_id)


@router.patch("/{check_id}")
async def update_check(
    check_id: int, body: CheckPatch, user_id: UserId, session: Session
) -> CheckOut:
    """State is kept: the next run uses the new config and decides."""
    check, _ = await _lock(session, user_id, check_id)
    changes = body.model_dump(exclude_unset=True)
    current = {
        "name": check.name,
        "url": check.url,
        "interval_s": check.interval_s,
        "timeout_s": float(check.timeout_s),
        "expected_status": check.expected_status,
        "expected_body": check.expected_body,
        "group_id": check.group_id,
        "alert_emails": check.alert_emails,
    }
    try:
        merged = CheckIn.model_validate(current | changes)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        raise RequestValidationError([e | {"loc": ("body", *e["loc"])} for e in errors]) from None
    if merged.group_id is not None and merged.group_id != check.group_id:
        await _hold_group(session, user_id, merged.group_id)

    old_interval = check.interval_s
    for field, value in _config(merged).items():
        setattr(check, field, value)
    if merged.interval_s != old_interval:
        # Keep the schedule anchored to the last slot. Not while a run holds the lease (its
        # completion schedules with the new interval) or during a retry streak (retries
        # keep their own spacing).
        shift = timedelta(seconds=merged.interval_s - old_interval)
        await session.execute(
            update(CheckState)
            .where(
                CheckState.check_id == check_id,
                _lease_free,
                CheckState.state != MonitorState.FAILING,
            )
            .values(next_run_at=func.greatest(func.now(), CheckState.next_run_at + shift))
        )
    await session.commit()
    return await _get(session, user_id, check_id)


@router.delete("/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_check(check_id: int, user_id: UserId, session: Session) -> None:
    """Soft delete: housekeeping purges the row later."""
    check, state = await _lock(session, user_id, check_id)
    await session.execute(update(Check).where(Check.id == check_id).values(deleted_at=func.now()))
    await _reset(session, check, state, IncidentEndReason.CHECK_DELETED)
    await session.commit()


@router.post("/{check_id}/pause", status_code=status.HTTP_204_NO_CONTENT)
async def pause_check(check_id: int, user_id: UserId, session: Session) -> None:
    """Ends an open incident without an UP email. A run in flight is discarded."""
    check, state = await _lock(session, user_id, check_id)
    if not check.is_paused:
        await session.execute(update(Check).where(Check.id == check_id).values(is_paused=True))
        await _reset(session, check, state, IncidentEndReason.PAUSED)
    await session.commit()


@router.post("/{check_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_check(check_id: int, user_id: UserId, session: Session) -> None:
    """A fresh start: pending, and due right away."""
    check, state = await _lock(session, user_id, check_id)
    if check.is_paused:
        await session.execute(update(Check).where(Check.id == check_id).values(is_paused=False))
        await session.execute(
            update(CheckState).where(CheckState.check_id == check_id).values(next_run_at=func.now())
        )
        await session.execute(
            notify_check(check.id, check.owner_id, state.state, state.last_response_ms)
        )
    await session.commit()


@router.post(
    "/{check_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "the check is paused or running"}},
)
async def run_check(check_id: int, user_id: UserId, session: Session) -> Response:
    """Make the check due now. The worker picks it up within a second."""
    paused = (
        await session.execute(
            select(Check.is_paused).where(
                Check.id == check_id, Check.owner_id == user_id, Check.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if paused is None:
        raise _not_found()
    if paused:
        raise HTTPException(status.HTTP_409_CONFLICT, "check is paused")
    made_due = (
        await session.execute(
            update(CheckState)
            .where(CheckState.check_id == check_id, _lease_free)
            .values(next_run_at=func.now())
            .returning(CheckState.check_id)
        )
    ).one_or_none()
    if made_due is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "check is running")
    await session.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)
