"""Telegram heads-ups to the operator about other users' sign-ups and checks."""

from collections.abc import AsyncGenerator

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from api.main import create_app
from core.settings import Settings
from core.telegram import TelegramError, TelegramMessage
from tests.conftest import SignIn, create_check

OPERATOR = "alice@x.io"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[TelegramMessage] = []
        self.fail = False

    async def __call__(self, msg: TelegramMessage) -> None:
        if self.fail:
            raise TelegramError(502, "Bad Gateway")
        self.sent.append(msg)

    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def app(
    engine: AsyncEngine, settings: Settings, telegram: FakeTelegram
) -> AsyncGenerator[FastAPI]:
    """The API with Telegram on, `alice@x.io` as the operator. Overrides conftest's `app`, so
    `alice`, `bob` and `sign_in` use it."""
    configured = settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("123:abc"),
            "telegram_chat_id": "42",
            "telegram_owner_email": OPERATOR,
        }
    )
    app = create_app(configured, send_telegram=telegram)
    async with app.router.lifespan_context(app):
        yield app


async def test_another_users_sign_up_rings_the_operator(
    sign_in: SignIn, telegram: FakeTelegram
) -> None:
    await sign_in(OPERATOR)
    assert telegram.sent == []  # the operator's own sign-up

    await sign_in("Bob@X.io")
    [msg] = telegram.sent
    assert (msg.chat_id, msg.silent) == ("42", False)
    assert msg.text == "👤 Bob@x.io\n🆕 Signed up\nUsers now: 2"  # domain normalised


async def test_another_users_new_check_shows_the_target_and_recipients(
    alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, telegram: FakeTelegram
) -> None:
    telegram.sent.clear()
    await create_check(bob, name="Blog", url="https://blog.example", alert_emails=["v@y.io"])
    assert telegram.texts() == [
        "👤 bob@x.io\n🔎 New check: Blog\nhttps://blog.example\nAlerts: v@y.io"
    ]

    await create_check(alice)
    assert len(telegram.sent) == 1  # the operator's own check


async def test_check_edits_notify_only_when_target_or_recipients_change(
    bob: httpx2.AsyncClient, telegram: FakeTelegram
) -> None:
    check = await create_check(bob, name="Blog", url="https://blog.example")
    path = f"/api/checks/{check['id']}"
    telegram.sent.clear()

    assert (await bob.patch(path, json={"name": "Blog 2", "interval_s": 120})).status_code == 200
    assert telegram.sent == []

    assert (await bob.patch(path, json={"url": "https://other.example"})).status_code == 200
    assert (await bob.patch(path, json={"alert_emails": ["x@z.io", "y@z.io"]})).status_code == 200
    group = (await bob.post("/api/groups", json={"name": "g", "alert_emails": ["g@z.io"]})).json()
    assert (await bob.patch(path, json={"group_id": group["id"]})).status_code == 200

    assert [t.splitlines()[1:] for t in telegram.texts()] == [
        ["✏️ Check changed: Blog 2", "https://other.example", "Alerts: alice@x.io"],
        ["✏️ Check changed: Blog 2", "https://other.example", "Alerts: x@z.io, y@z.io"],
        ["✏️ Check changed: Blog 2", "https://other.example", "Alerts: g@z.io"],
    ]


async def test_group_alert_changes_notify_when_the_group_has_checks(
    bob: httpx2.AsyncClient, telegram: FakeTelegram
) -> None:
    group = (await bob.post("/api/groups", json={"name": "g", "alert_emails": ["a@z.io"]})).json()
    path = f"/api/groups/{group['id']}"
    telegram.sent.clear()

    assert (await bob.patch(path, json={"alert_emails": ["b@z.io"]})).status_code == 200
    assert telegram.sent == []  # no checks: nobody gets alerted from it yet

    await create_check(bob, group_id=group["id"])
    telegram.sent.clear()
    assert (await bob.patch(path, json={"name": "renamed"})).status_code == 200
    assert telegram.sent == []
    assert (await bob.patch(path, json={"alert_emails": ["c@z.io"]})).status_code == 200
    assert telegram.texts() == [
        "👤 bob@x.io\n✏️ Group alerts changed: renamed\nAlerts: c@z.io\nChecks in the group: 1"
    ]


async def test_a_failed_notification_never_fails_the_request(
    sign_in: SignIn, telegram: FakeTelegram
) -> None:
    telegram.fail = True
    client = await sign_in("bob@x.io")  # asserts 201
    await create_check(client)  # asserts 201
