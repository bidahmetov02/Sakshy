"""The notification outbox (`design/sequences.md` (b)): enqueueing and sending alert emails.

Rows are queued in the same transaction as the incident change that causes them, one per
recipient. The sender claims due rows, sends each as its own email and records the
outcome. Delivery is at-least-once: a crash between the SMTP send and marking the row sent
re-sends it once the claim expires.
"""

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

import aiosmtplib
from sqlalchemy import ColumnElement, and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from core.models import (
    AlertKind,
    Check,
    CheckGroup,
    Incident,
    Notification,
    OutboxStatus,
)
from core.settings import Settings
from worker.emails import Alert, render

log = logging.getLogger("worker.outbox")

CLAIM_LEASE = timedelta(seconds=60)  # a crashed sender's rows become due again after this
# Waits after each failed attempt, doubling up to 30 min: the last of the 8 attempts starts
# ~1 h after the first.
RETRY_DELAYS = tuple(timedelta(seconds=s) for s in (30, 60, 120, 240, 480, 960, 1800))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
BATCH = 20
MAX_ERROR = 500

type Send = Callable[[EmailMessage], Awaitable[Any]]


def alert_recipients() -> ColumnElement[list[str]]:
    """A grouped check alerts its group's list, which replaces the check's own. Callers
    join `CheckGroup` on `Check.group_id` (outer)."""
    return func.coalesce(CheckGroup.alert_emails, Check.alert_emails)


async def enqueue(
    conn: AsyncConnection, incident_id: int, kind: AlertKind, recipients: list[str]
) -> None:
    rows = [{"incident_id": incident_id, "kind": kind, "recipient": r} for r in recipients]
    await conn.execute(pg_insert(Notification).values(rows).on_conflict_do_nothing())


async def claim(engine: AsyncEngine, limit: int) -> list[Alert]:
    """Claim due rows: push `next_attempt_at` out by the lease and count the attempt.

    An UP email waits while the DOWN email to the same recipient for the same incident is
    still pending (in backoff, or being sent), so UP never arrives before DOWN.
    """
    n, down = aliased(Notification), aliased(Notification)
    down_pending = exists().where(
        down.incident_id == n.incident_id,
        down.recipient == n.recipient,
        down.kind == AlertKind.DOWN,
        down.status == OutboxStatus.PENDING,
    )
    due = (
        select(n.id)
        .where(
            n.status == OutboxStatus.PENDING,
            n.next_attempt_at <= func.now(),
            ~and_(n.kind == AlertKind.UP, down_pending),
        )
        .order_by(n.next_attempt_at, n.id)
        .limit(limit)
        .with_for_update(of=n, skip_locked=True)
    )
    stmt = (
        update(Notification)
        .where(
            Notification.id.in_(due),
            Notification.incident_id == Incident.id,
            Incident.check_id == Check.id,
        )
        .values(
            next_attempt_at=func.now() + CLAIM_LEASE,
            attempts=Notification.attempts + 1,
        )
        .returning(
            Notification.id,
            Notification.attempts,
            Notification.kind,
            Notification.recipient,
            Check.id,
            Check.name,
            Check.url,
            Incident.started_at,
            Incident.detected_at,
            Incident.ended_at,
            Incident.end_reason,
            Incident.cause_kind,
            Incident.cause_message,
        )
    )
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).all()
    return sorted((Alert(*row) for row in rows), key=lambda a: a.notification_id)


class Outcome(enum.Enum):
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"


async def record(
    engine: AsyncEngine, alert: Alert, outcome: Outcome, error: str | None = None
) -> bool:
    """Store a send's outcome. Fenced on the attempt count: if the claim expired and another
    sender took the row over, this stale outcome is dropped (returns False)."""
    n = Notification
    match outcome:
        case Outcome.SENT:
            values: dict[str, Any] = {"status": OutboxStatus.SENT, "sent_at": func.now()}
        case Outcome.RETRY:
            delay = RETRY_DELAYS[min(alert.attempts, len(RETRY_DELAYS)) - 1]
            values = {"next_attempt_at": func.now() + delay}
        case Outcome.FAILED:
            values = {"status": OutboxStatus.FAILED}
    stmt = (
        update(n)
        .where(
            n.id == alert.notification_id,
            n.status == OutboxStatus.PENDING,
            n.attempts == alert.attempts,
        )
        .values(last_error=error, **values)
    )
    async with engine.begin() as conn:
        return (await conn.execute(stmt)).rowcount == 1


def is_permanent(exc: Exception) -> bool:
    """5xx replies won't change on retry; 4xx replies, timeouts and connection errors may."""
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        return all(r.code >= 500 for r in exc.recipients)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return exc.code >= 500
    return False


async def send_due(
    engine: AsyncEngine, settings: Settings, send: Send, stop: asyncio.Event | None = None
) -> int:
    """Claim one batch and send it, one email at a time. Returns the number sent."""
    sent = 0
    for alert in await claim(engine, BATCH):
        if stop is not None and stop.is_set():
            break  # the rest become due again when their claim expires
        try:
            await send(render(alert, settings.mail_from, settings.app_base_url))
        except Exception as exc:
            final = is_permanent(exc) or alert.attempts >= MAX_ATTEMPTS
            error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
            await record(engine, alert, Outcome.FAILED if final else Outcome.RETRY, error)
            log.warning(
                "%s email %d to %s, attempt %d %s: %s",
                alert.kind.value,
                alert.notification_id,
                alert.recipient,
                alert.attempts,
                "failed for good" if final else "will retry",
                error,
            )
        else:
            await record(engine, alert, Outcome.SENT)
            sent += 1
    return sent


def smtp_sender(settings: Settings) -> Send:
    """One SMTP connection per email: alert volume is low, and a broken connection can
    never take later emails down with it."""

    async def send(msg: EmailMessage) -> None:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            timeout=settings.smtp_timeout_s,
        )

    return send
