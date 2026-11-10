"""Sending Telegram messages through the Bot API: used by the worker (alerts) and the API
(operator heads-ups). The token is part of every request URL, so it is kept out of logs and
error messages."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from core.settings import Settings

TIMEOUT_S = 10.0
MAX_ERROR_BODY = 200


@dataclass(frozen=True)
class TelegramMessage:
    chat_id: str
    text: str
    silent: bool


type SendTelegram = Callable[[TelegramMessage], Awaitable[Any]]


class TelegramError(Exception):
    """The Bot API refused a message. `code` follows HTTP: 429 and 5xx may succeed later."""

    def __init__(self, code: int, description: str) -> None:
        super().__init__(f"{code} {description}")
        self.code = code


def telegram_sender(settings: Settings, client: httpx2.AsyncClient) -> SendTelegram:
    if settings.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    token = settings.telegram_bot_token.get_secret_value()
    # httpx2 logs every request URL at INFO, and this one carries the token.
    httpx_log = logging.getLogger("httpx2")
    if not any(isinstance(f, _Redact) and f.secret == token for f in httpx_log.filters):
        httpx_log.addFilter(_Redact(token))
    url = f"{settings.telegram_api_url.rstrip('/')}/bot{token}/sendMessage"

    async def send(msg: TelegramMessage) -> None:
        try:
            response = await client.post(
                url,
                json={
                    "chat_id": msg.chat_id,
                    "text": msg.text,
                    "disable_notification": msg.silent,
                    # The checked site's preview would crowd out the alert.
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=TIMEOUT_S,
            )
        except httpx2.HTTPError as exc:
            # Callers store and log the error: keep the token out of it.
            error = f"{type(exc).__name__}: {exc}".replace(token, "<redacted>")
            raise ConnectionError(error) from None
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        if not (response.is_success and body.get("ok")):
            description = str(body.get("description") or response.text[:MAX_ERROR_BODY])
            raise TelegramError(int(body.get("error_code") or response.status_code), description)

    return send


class _Redact(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.secret in message:
            record.msg, record.args = message.replace(self.secret, "<redacted>"), None
        return True
