"""Public status pages: no auth, looked up by slug. A disabled or unknown page is a 404.

Never shows a URL, a check id or an error message. Fields the page's flags don't allow are
left out of the response. Deleted checks are left out.
"""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Session
from api.queries import RangeQuery, maintenance_since, stats, tiered_status, uptime_pct
from api.schemas import PublicIncident, PublicItem, PublicPage, PublicState, Stats
from core.models import Check, CheckState, Incident, MonitorState, StatusPage, StatusPageItem

router = APIRouter(prefix="/public", tags=["public"])

INCIDENTS_SHOWN = 20
INCIDENTS_SINCE = timedelta(days=30)

item = StatusPageItem


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "status page not found")


async def _page(session: AsyncSession, slug: str) -> StatusPage:
    page = (
        await session.scalars(
            select(StatusPage).where(StatusPage.slug == slug, StatusPage.is_enabled)
        )
    ).one_or_none()
    if page is None:
        raise _not_found()
    return page


def _public_state(
    is_paused: bool,
    in_maintenance_since: datetime | None,
    state: MonitorState,
    state_changed_at: datetime,
    incident_started_at: datetime | None,
) -> tuple[PublicState, datetime]:
    """Paused, then maintenance, then down. `failing` is unconfirmed, so it shows as up."""
    if is_paused:
        return "paused", state_changed_at
    if in_maintenance_since is not None:
        return "maintenance", in_maintenance_since
    match state:
        case MonitorState.DOWN:
            return "down", incident_started_at or state_changed_at
        case MonitorState.UP | MonitorState.FAILING:
            return "up", state_changed_at
        case MonitorState.PENDING:
            return "pending", state_changed_at


@router.get("/{slug}", response_model_exclude_unset=True)
async def public_page(slug: str, session: Session) -> PublicPage:
    page = await _page(session, slug)
    rows = (
        await session.execute(
            select(
                item.id,
                item.display_name,
                Check.id,
                Check.is_paused,
                maintenance_since(),
                CheckState.state,
                CheckState.state_changed_at,
                Incident.started_at,
                CheckState.last_response_ms,
            )
            .join(Check, Check.id == item.check_id)
            .join(CheckState, CheckState.check_id == Check.id)
            .outerjoin(Incident, and_(Incident.check_id == Check.id, Incident.ended_at.is_(None)))
            .where(item.status_page_id == page.id, Check.deleted_at.is_(None))
            .order_by(item.position, item.id)
        )
    ).all()
    uptimes = await uptime_pct(session, [row[2] for row in rows]) if page.show_uptime else {}

    items: list[PublicItem] = []
    counted = down = 0
    for item_id, name, check_id, paused, maintenance, state_, changed_at, started_at, rt in rows:
        state, since = _public_state(paused, maintenance, state_, changed_at, started_at)
        if state not in ("paused", "maintenance"):
            counted += 1
            down += state == "down"
        fields: dict[str, object] = {}
        if page.show_uptime:
            fields["uptime_pct"] = uptimes.get(check_id)
        if page.show_response_times:
            fields["last_response_ms"] = rt
        items.append(
            PublicItem(id=item_id, display_name=name, state=state, since=since, **fields)  # pyright: ignore[reportArgumentType]
        )

    result = PublicPage(title=page.title, overall_status=tiered_status(counted, down), items=items)
    if page.show_incidents:
        result.incidents = await _incidents(session, page.id)
    return result


async def _incidents(session: AsyncSession, page_id: int) -> list[PublicIncident]:
    """The latest incidents of the listed checks that overlap the last 30 days."""
    rows = await session.execute(
        select(item.id, item.display_name, Incident.started_at, Incident.ended_at)
        .join(Check, Check.id == item.check_id)
        .join(Incident, Incident.check_id == Check.id)
        .where(
            item.status_page_id == page_id,
            Check.deleted_at.is_(None),
            or_(Incident.ended_at.is_(None), Incident.ended_at >= func.now() - INCIDENTS_SINCE),
        )
        .order_by(Incident.started_at.desc(), Incident.id.desc())
        .limit(INCIDENTS_SHOWN)
    )
    return [
        PublicIncident(item_id=item_id, display_name=name, started_at=started, ended_at=ended)
        for item_id, name, started, ended in rows
    ]


@router.get("/{slug}/stats", response_model_exclude_unset=True)
async def public_stats(
    slug: str,
    session: Session,
    item_id: Annotated[int, Query(alias="item")],
    range_: RangeQuery = "day",
) -> Stats:
    """One item's chart (`item` is the item's `id`), only the fields the page shows."""
    page = await _page(session, slug)
    if not (page.show_uptime or page.show_response_times):
        raise _not_found()
    check_id = (
        await session.execute(
            select(Check.id)
            .join(item, item.check_id == Check.id)
            .where(item.id == item_id, item.status_page_id == page.id, Check.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if check_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return await stats(
        session,
        check_id,
        range_,
        uptime=page.show_uptime,
        response_times=page.show_response_times,
    )
