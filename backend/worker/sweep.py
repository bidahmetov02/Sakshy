"""Maintenance sweep (`design/sequences.md` (b)): a DOWN email suppressed by a maintenance
window is queued once no window applies any more and the incident is still open."""

import logging

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import AlertKind, Check, CheckGroup, CheckState, Incident, MaintenanceWindow
from worker.outbox import alert_recipients, enqueue

log = logging.getLogger("worker.sweep")


async def sweep(engine: AsyncEngine) -> int:
    """Queue the DOWN emails that are due. Returns the number of incidents alerted.

    Locks `check_state` first (the lock-order rule) with SKIP LOCKED: a check being
    completed right now is skipped until the next round. Otherwise the sweep could queue
    DOWN while that completion closes the incident without an UP email.
    """
    w = MaintenanceWindow
    in_maintenance = exists().where(
        w.starts_at <= func.now(),
        w.ends_at > func.now(),
        or_(w.check_id == Check.id, w.group_id == Check.group_id),
    )
    due = (
        select(Incident.id, alert_recipients())
        .select_from(CheckState)
        .join(Check, Check.id == CheckState.check_id)
        .outerjoin(CheckGroup, CheckGroup.id == Check.group_id)
        .join(Incident, Incident.check_id == CheckState.check_id)
        .where(Incident.ended_at.is_(None), ~Incident.down_alert_queued, ~in_maintenance)
        .with_for_update(of=[CheckState, Incident], skip_locked=True)
    )
    async with engine.begin() as conn:
        rows = (await conn.execute(due)).all()
        for incident_id, recipients in rows:
            await enqueue(conn, incident_id, AlertKind.DOWN, recipients)
        if rows:
            await conn.execute(
                update(Incident)
                .where(Incident.id.in_([incident_id for incident_id, _ in rows]))
                .values(down_alert_queued=True)
            )
    if rows:
        log.info("maintenance over: queued DOWN emails for %d incidents", len(rows))
    return len(rows)
