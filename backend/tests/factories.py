"""Test rows, inserted the way the API will (a check always comes with its state row),
and read back."""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.models import (
    AlertKind,
    Check,
    CheckGroup,
    CheckState,
    ErrorKind,
    Incident,
    MaintenanceWindow,
    Notification,
    User,
)


async def add_user(engine: AsyncEngine, email: str = "alice@x.io") -> int:
    async with engine.begin() as conn:
        stmt = insert(User).values(email=email, password_hash="x").returning(User.id)
        return (await conn.execute(stmt)).scalar_one()


async def add_group(
    engine: AsyncEngine, owner_id: int, name: str = "prod", alert_emails: list[str] | None = None
) -> int:
    values = {"owner_id": owner_id, "name": name, "alert_emails": alert_emails or ["ops@x.io"]}
    async with engine.begin() as conn:
        stmt = insert(CheckGroup).values(values).returning(CheckGroup.id)
        return (await conn.execute(stmt)).scalar_one()


async def add_check(engine: AsyncEngine, owner_id: int, **overrides: Any) -> int:
    values = {
        "owner_id": owner_id,
        "name": "site",
        "url": "http://127.0.0.1:9000/ok",
        "interval_s": 60,
        "timeout_s": Decimal(10),
        "alert_emails": ["alice@x.io"],
    } | overrides
    async with engine.begin() as conn:
        check_id = (
            await conn.execute(insert(Check).values(values).returning(Check.id))
        ).scalar_one()
        await conn.execute(insert(CheckState).values(check_id=check_id))
    return check_id


async def add_window(engine: AsyncEngine, owner_id: int, **target: Any) -> int:
    """A maintenance window active from an hour ago to an hour from now."""
    values = {
        "owner_id": owner_id,
        "starts_at": func.now() - text("interval '1 hour'"),
        "ends_at": func.now() + text("interval '1 hour'"),
    } | target
    async with engine.begin() as conn:
        stmt = insert(MaintenanceWindow).values(values).returning(MaintenanceWindow.id)
        return (await conn.execute(stmt)).scalar_one()


async def rows[T](engine: AsyncEngine, model: type[T]) -> list[T]:
    async with AsyncSession(engine) as session:
        return list((await session.scalars(select(model))).all())


async def state_of(engine: AsyncEngine, check_id: int) -> CheckState:
    async with AsyncSession(engine) as session:
        state = await session.get(CheckState, check_id)
        assert state is not None
        return state


async def add_incident(engine: AsyncEngine, check_id: int, **overrides: Any) -> int:
    """An open incident that started ten minutes ago and was alerted."""
    values = {
        "check_id": check_id,
        "started_at": func.now() - text("interval '10 min'"),
        "detected_at": func.now() - text("interval '9 min'"),
        "cause_kind": ErrorKind.TIMEOUT,
        "cause_message": "no response within 10 s",
        "down_alert_queued": True,
    } | overrides
    async with engine.begin() as conn:
        stmt = insert(Incident).values(values).returning(Incident.id)
        return (await conn.execute(stmt)).scalar_one()


async def add_notification(
    engine: AsyncEngine, incident_id: int, recipient: str, kind: AlertKind = AlertKind.DOWN
) -> int:
    async with engine.begin() as conn:
        stmt = (
            insert(Notification)
            .values(incident_id=incident_id, kind=kind, recipient=recipient)
            .returning(Notification.id)
        )
        return (await conn.execute(stmt)).scalar_one()
