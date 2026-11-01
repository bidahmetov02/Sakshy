"""The outbox sender against real Postgres: claim lease, UP-after-DOWN ordering, retries,
permanent failures, fencing. Delivery itself goes to the compose Mailpit."""

import asyncio
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

import aiosmtplib
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.models import AlertKind, IncidentEndReason, Notification, OutboxStatus
from core.settings import Settings
from tests.factories import add_check, add_incident, add_notification, add_user
from tests.mailpit import Mailpit, new_address
from worker.outbox import (
    CLAIM_LEASE,
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    Outcome,
    claim,
    record,
    send_due,
    smtp_sender,
)


class FakeSMTP:
    """Records what it sends; raises the given errors first, one per call."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.sent: list[EmailMessage] = []

    async def __call__(self, msg: EmailMessage) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(msg)


@pytest.fixture
async def check(engine: AsyncEngine) -> int:
    return await add_check(engine, await add_user(engine), name="Shop", url="https://shop.example")


@pytest.fixture
async def incident(engine: AsyncEngine, check: int) -> int:
    return await add_incident(engine, check)


async def sql(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def make_due(engine: AsyncEngine) -> None:
    await sql(engine, "UPDATE notifications SET next_attempt_at = now()")


async def row(engine: AsyncEngine, notification_id: int) -> Notification:
    async with AsyncSession(engine) as session:
        found = await session.get(Notification, notification_id)
        assert found is not None
        return found


async def due_in(engine: AsyncEngine, notification_id: int) -> timedelta:
    async with engine.connect() as conn:
        stmt = select(Notification.next_attempt_at - text("now()")).where(
            Notification.id == notification_id
        )
        return (await conn.execute(stmt)).scalar_one()


def about(delta: timedelta, expected: timedelta) -> bool:
    return expected - timedelta(seconds=2) < delta <= expected


# ---------- Claim ----------


async def test_claim_leases_due_rows_and_counts_the_attempt(
    engine: AsyncEngine, check: int, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    [alert] = await claim(engine, 10)

    assert (alert.notification_id, alert.attempts, alert.kind, alert.recipient) == (
        n,
        1,
        AlertKind.DOWN,
        "a@x.io",
    )
    assert (alert.check_id, alert.check_name, alert.url) == (check, "Shop", "https://shop.example")
    assert alert.cause_message == "no response within 10 s"
    assert about(await due_in(engine, n), CLAIM_LEASE)
    assert await claim(engine, 10) == []  # leased


async def test_expired_claim_is_due_again_as_another_attempt(
    engine: AsyncEngine, incident: int
) -> None:
    await add_notification(engine, incident, "a@x.io")
    await claim(engine, 10)
    await make_due(engine)  # the sender crashed: its claim ran out
    [alert] = await claim(engine, 10)
    assert alert.attempts == 2


async def test_claim_skips_future_sent_and_failed_rows(engine: AsyncEngine, incident: int) -> None:
    future, sent, failed = [await add_notification(engine, incident, f"{r}@x.io") for r in "abc"]
    await sql(
        engine,
        "UPDATE notifications SET next_attempt_at = now() + interval '1 min' WHERE id = :id",
        id=future,
    )
    await sql(engine, "UPDATE notifications SET status = 'sent' WHERE id = :id", id=sent)
    await sql(engine, "UPDATE notifications SET status = 'failed' WHERE id = :id", id=failed)
    assert await claim(engine, 10) == []


async def test_claim_takes_the_oldest_due_first_up_to_the_limit(
    engine: AsyncEngine, incident: int
) -> None:
    ids = [await add_notification(engine, incident, f"{r}@x.io") for r in "abc"]
    assert [a.notification_id for a in await claim(engine, 2)] == ids[:2]


async def test_concurrent_senders_claim_disjoint_rows(engine: AsyncEngine, incident: int) -> None:
    ids = {await add_notification(engine, incident, f"r{i}@x.io") for i in range(30)}
    batches = await asyncio.gather(*(claim(engine, 30) for _ in range(4)))
    claimed = [a.notification_id for batch in batches for a in batch]
    assert sorted(claimed) == sorted(ids)  # each row exactly once


async def test_up_waits_for_the_pending_down_to_the_same_recipient(
    engine: AsyncEngine, check: int
) -> None:
    incident = await add_incident(
        engine, check, ended_at=text("now()"), end_reason=IncidentEndReason.RECOVERED
    )
    down_a = await add_notification(engine, incident, "a@x.io", AlertKind.DOWN)
    up_a = await add_notification(engine, incident, "a@x.io", AlertKind.UP)
    up_b = await add_notification(engine, incident, "b@x.io", AlertKind.UP)  # got no DOWN

    first = await claim(engine, 10)
    assert {a.notification_id for a in first} == {down_a, up_b}
    # Still held back while the DOWN is claimed but unsent, or in backoff.
    await sql(
        engine,
        "UPDATE notifications SET next_attempt_at = now() + interval '1 h' WHERE id = :id",
        id=down_a,
    )
    assert await claim(engine, 10) == []

    await sql(engine, "UPDATE notifications SET status = 'sent' WHERE id = :id", id=down_a)
    assert [a.notification_id for a in await claim(engine, 10)] == [up_a]


async def test_up_goes_out_after_the_down_failed_for_good(engine: AsyncEngine, check: int) -> None:
    incident = await add_incident(
        engine, check, ended_at=text("now()"), end_reason=IncidentEndReason.RECOVERED
    )
    down = await add_notification(engine, incident, "a@x.io", AlertKind.DOWN)
    up = await add_notification(engine, incident, "a@x.io", AlertKind.UP)
    await sql(engine, "UPDATE notifications SET status = 'failed' WHERE id = :id", id=down)
    assert [a.notification_id for a in await claim(engine, 10)] == [up]


# ---------- Sending and outcomes ----------


async def test_sent_email_is_marked_sent(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    smtp = FakeSMTP()
    assert await send_due(engine, settings, smtp) == 1

    [msg] = smtp.sent
    assert (msg["To"], msg["Subject"]) == ("a@x.io", "[Sakshy] DOWN: Shop")
    sent = await row(engine, n)
    assert (sent.status, sent.attempts, sent.last_error) == (OutboxStatus.SENT, 1, None)
    assert sent.sent_at is not None


async def test_transient_error_retries_after_a_backoff(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    assert (
        await send_due(engine, settings, FakeSMTP(aiosmtplib.SMTPServerDisconnected("gone"))) == 0
    )

    retry = await row(engine, n)
    assert (retry.status, retry.attempts) == (OutboxStatus.PENDING, 1)
    assert retry.last_error == "SMTPServerDisconnected: gone"
    assert about(await due_in(engine, n), RETRY_DELAYS[0])


async def test_backoff_doubles_then_the_row_fails_after_the_last_attempt(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    busy = aiosmtplib.SMTPResponseException(451, "try again later")
    delays: list[timedelta] = []
    for _ in range(MAX_ATTEMPTS):
        await make_due(engine)
        await send_due(engine, settings, FakeSMTP(busy))
        if (await row(engine, n)).status is OutboxStatus.PENDING:
            delays.append(await due_in(engine, n))

    assert len(delays) == len(RETRY_DELAYS)
    assert all(about(d, e) for d, e in zip(delays, RETRY_DELAYS, strict=True))
    assert [d.total_seconds() for d in RETRY_DELAYS] == [30, 60, 120, 240, 480, 960, 1800]
    failed = await row(engine, n)
    assert (failed.status, failed.attempts) == (OutboxStatus.FAILED, MAX_ATTEMPTS)
    assert failed.last_error == "SMTPResponseException: (451, 'try again later')"


@pytest.mark.parametrize(
    "error",
    [
        aiosmtplib.SMTPRecipientsRefused(
            [aiosmtplib.SMTPRecipientRefused(550, "no such user", "a@x.io")]
        ),
        aiosmtplib.SMTPResponseException(554, "rejected"),
    ],
    ids=["recipient refused", "5xx reply"],
)
async def test_permanent_error_fails_at_once(
    engine: AsyncEngine, settings: Settings, incident: int, error: Exception
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    await send_due(engine, settings, FakeSMTP(error))
    failed = await row(engine, n)
    assert (failed.status, failed.attempts) == (OutboxStatus.FAILED, 1)


async def test_temporarily_refused_recipient_is_retried(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    error = aiosmtplib.SMTPRecipientsRefused(
        [aiosmtplib.SMTPRecipientRefused(450, "mailbox busy", "a@x.io")]
    )
    await send_due(engine, settings, FakeSMTP(error))
    assert (await row(engine, n)).status is OutboxStatus.PENDING


async def test_a_stale_outcome_is_fenced_out(engine: AsyncEngine, incident: int) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    [slow] = await claim(engine, 10)
    await make_due(engine)
    [newer] = await claim(engine, 10)  # the first claim expired: another sender took over

    assert not await record(engine, slow, Outcome.RETRY, "late")
    assert await record(engine, newer, Outcome.SENT)
    assert not await record(engine, slow, Outcome.FAILED, "late")
    sent = await row(engine, n)
    assert (sent.status, sent.last_error) == (OutboxStatus.SENT, None)


async def test_stop_leaves_the_rest_of_the_batch_to_expire(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    stop = asyncio.Event()
    stop.set()
    smtp = FakeSMTP()
    assert await send_due(engine, settings, smtp, stop) == 0

    assert smtp.sent == []
    unsent = await row(engine, n)
    assert (unsent.status, unsent.attempts) == (OutboxStatus.PENDING, 1)
    assert about(await due_in(engine, n), CLAIM_LEASE)


# ---------- Real SMTP ----------


async def test_delivers_through_smtp(
    engine: AsyncEngine, settings: Settings, incident: int, mailpit: Mailpit
) -> None:
    to = new_address()
    n = await add_notification(engine, incident, to)
    assert await send_due(engine, settings, smtp_sender(settings)) == 1

    [msg] = await mailpit.wait_for(to, 1)
    assert msg["Subject"] == "[Sakshy] DOWN: Shop"
    assert [a["Address"] for a in msg["To"]] == [to]
    assert msg["MessageID"] == f"notification-{n}@sakshy.local"
    assert "URL:        https://shop.example" in msg["Text"]
    assert (await row(engine, n)).status is OutboxStatus.SENT


async def test_unreachable_smtp_server_is_retried(
    engine: AsyncEngine, settings: Settings, incident: int
) -> None:
    n = await add_notification(engine, incident, "a@x.io")
    closed = settings.model_copy(update={"smtp_port": 1, "smtp_timeout_s": 2.0})
    await send_due(engine, closed, smtp_sender(closed))

    retry = await row(engine, n)
    assert retry.status is OutboxStatus.PENDING
    assert retry.last_error is not None
    assert retry.last_error.startswith("SMTPConnectError")
