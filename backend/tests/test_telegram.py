"""Telegram alert content and the Bot API sender, against a mock transport (no network)."""

import json
import logging
from dataclasses import replace
from datetime import timedelta

import httpx2
import pytest
from pydantic import SecretStr

from core.models import AlertKind, IncidentEndReason
from core.settings import Settings
from core.telegram import TelegramError, TelegramMessage, telegram_sender
from tests.test_emails import BASE, DOWN, T
from worker.outbox import is_permanent
from worker.telegram import recipient, render

TOKEN = "123456:secret-bot-token"
OPERATOR = "Me@X.io"
MINE = replace(DOWN, recipient="telegram:42", owner_email="me@x.io")
THEIRS = replace(MINE, owner_email="bob@y.io")
RECOVERED = {
    "kind": AlertKind.UP,
    "ended_at": T + timedelta(minutes=12, seconds=5),
    "end_reason": IncidentEndReason.RECOVERED,
}


def tg_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": SecretStr(TOKEN),
        "telegram_chat_id": "42",
        "telegram_owner_email": OPERATOR,
    } | overrides
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue, reportArgumentType]


# ---------- Content ----------


def test_own_down_rings_and_links_to_the_check() -> None:
    msg = render(MINE, OPERATOR, BASE)
    assert msg == TelegramMessage(
        chat_id="42",
        text="\n".join(
            [
                "🔴 DOWN: Shop",
                "https://shop.example",
                "Cause: timeout: no response within 10 s",
                "Down since: 2026-10-30 14:20:05 UTC",
                "→ http://localhost:8080/checks/17",
            ]
        ),
        silent=False,
    )


def test_someone_elses_down_is_silent_names_the_owner_and_has_no_link() -> None:
    msg = render(THEIRS, OPERATOR, BASE)
    assert msg.silent
    assert msg.text.splitlines()[:2] == ["👤 bob@y.io", "🔴 DOWN: Shop"]
    assert "/checks/" not in msg.text


def test_up_shows_the_downtime() -> None:
    mine = render(replace(MINE, **RECOVERED), OPERATOR, BASE)
    assert mine.text.splitlines()[:3] == [
        "🟢 UP: Shop (down 12m 5s)",
        "https://shop.example",
        "Recovered: 2026-10-30 14:32:10 UTC",
    ]
    assert not mine.silent
    assert render(replace(THEIRS, **RECOVERED), OPERATOR, BASE).silent


def test_delayed_down_says_the_incident_already_ended() -> None:
    late = replace(MINE, ended_at=T + timedelta(hours=1), end_reason=IncidentEndReason.RECOVERED)
    assert "This alert was delayed: the incident ended at 2026-10-30 15:20:05 UTC (recovered)." in (
        render(late, OPERATOR, BASE).text
    )


def test_up_for_an_open_incident_is_a_bug() -> None:
    with pytest.raises(ValueError, match="open incident"):
        render(replace(MINE, kind=AlertKind.UP), OPERATOR, BASE)


def test_recipient_only_when_configured() -> None:
    assert recipient(tg_settings()) == "telegram:42"
    assert recipient(Settings(_env_file=None)) is None  # pyright: ignore[reportCallIssue]


# ---------- Sender ----------


class FakeBotAPI:
    """Answers like the Bot API; records each request."""

    def __init__(self, status: int = 200, body: dict[str, object] | str | None = None) -> None:
        self.status = status
        self.body: dict[str, object] | str = {"ok": True} if body is None else body
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.body, str):
            return httpx2.Response(self.status, text=self.body)
        return httpx2.Response(self.status, json=self.body)


async def send_with(api: FakeBotAPI | httpx2.MockTransport, msg: TelegramMessage) -> None:
    transport = api if isinstance(api, httpx2.MockTransport) else httpx2.MockTransport(api)
    async with httpx2.AsyncClient(transport=transport) as client:
        await telegram_sender(tg_settings(), client)(msg)


async def test_posts_send_message_to_the_bot_api() -> None:
    api = FakeBotAPI()
    await send_with(api, TelegramMessage(chat_id="42", text="hi", silent=True))

    [request] = api.requests
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(request.content) == {
        "chat_id": "42",
        "text": "hi",
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }


@pytest.mark.parametrize(
    ("status", "body", "code", "permanent"),
    [
        (
            403,
            {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
            403,
            True,
        ),
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            400,
            True,
        ),
        (429, {"ok": False, "error_code": 429, "description": "Too Many Requests"}, 429, False),
        (502, "<html>Bad Gateway</html>", 502, False),
    ],
    ids=["blocked", "no chat", "rate limited", "gateway"],
)
async def test_refusals_raise_with_the_code(
    status: int, body: dict[str, object] | str, code: int, permanent: bool
) -> None:
    with pytest.raises(TelegramError) as raised:
        await send_with(FakeBotAPI(status, body), TelegramMessage("42", "hi", silent=False))
    assert raised.value.code == code
    assert is_permanent(raised.value) is permanent


async def test_network_errors_are_retried_and_never_carry_the_token() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(ConnectionError) as raised:
        await send_with(httpx2.MockTransport(refuse), TelegramMessage("42", "hi", silent=False))
    assert TOKEN not in str(raised.value)
    assert "<redacted>" in str(raised.value)
    assert not is_permanent(raised.value)


async def test_request_logs_never_carry_the_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="httpx2"):
        await send_with(FakeBotAPI(), TelegramMessage("42", "hi", silent=False))
    assert "sendMessage" in caplog.text
    assert TOKEN not in caplog.text
