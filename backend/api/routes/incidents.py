"""The incident log, across the user's checks. Incidents of deleted checks are left out."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime
from sqlalchemy import or_, select, tuple_

from api.deps import Session, UserId
from api.queries import Limit, decode_cursor, encode_cursor
from api.schemas import IncidentOut, Page
from core.models import Check, Incident

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("")
async def list_incidents(
    user_id: UserId,
    session: Session,
    check_id: int | None = None,
    group_id: Annotated[int | None, Query(description="the checks in the group now")] = None,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: AwareDatetime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> Page[IncidentOut]:
    """Newest first. `from` and `to` select the incidents that overlap that period."""
    i = Incident
    stmt = (
        select(i)
        .join(Check, Check.id == i.check_id)
        .where(Check.owner_id == user_id, Check.deleted_at.is_(None))
        .order_by(i.started_at.desc(), i.id.desc())
        .limit(limit + 1)
    )
    if check_id is not None:
        stmt = stmt.where(i.check_id == check_id)
    if group_id is not None:
        stmt = stmt.where(Check.group_id == group_id)
    if from_ is not None:
        stmt = stmt.where(or_(i.ended_at.is_(None), i.ended_at >= from_))
    if to is not None:
        stmt = stmt.where(i.started_at < to)
    if cursor is not None:
        stmt = stmt.where(tuple_(i.started_at, i.id) < tuple_(*decode_cursor(cursor)))
    rows = list((await session.scalars(stmt)).all())
    items = [
        IncidentOut(
            id=incident.id,
            check_id=incident.check_id,
            started_at=incident.started_at,
            detected_at=incident.detected_at,
            ended_at=incident.ended_at,
            duration_s=None
            if incident.ended_at is None
            else round((incident.ended_at - incident.started_at).total_seconds()),
            end_reason=incident.end_reason,
            cause_kind=incident.cause_kind,
            cause_message=incident.cause_message,
            began_in_maintenance=incident.began_in_maintenance,
            alerted=incident.down_alert_queued,
        )
        for incident in rows[:limit]
    ]
    more = len(rows) > limit
    last = items[-1] if more else None
    return Page(items=items, next_cursor=last and encode_cursor(last.started_at, last.id))
