"""Telegram heads-ups to the operator about other users: sign-ups, and checks whose target
or alert recipients change. Sign-ups are open, and alert emails go out from the operator's
mail account, so these are the moments a stranger could start abusing the server.

Best effort: sent after the response, one try. A failure is logged and the message is lost.
The operator's own actions send nothing.
"""

import logging
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import User
from core.settings import Settings
from core.telegram import SendTelegram, TelegramMessage

log = logging.getLogger("api.notify")


class Notifier:
    def __init__(self, settings: Settings, send: SendTelegram | None) -> None:
        self.send = send if settings.telegram_enabled else None
        self.chat_id = settings.telegram_chat_id or ""
        self.operator = (settings.telegram_owner_email or "").lower()

    @property
    def enabled(self) -> bool:
        return self.send is not None

    def tell(self, background: BackgroundTasks, actor_email: str, lines: list[str]) -> None:
        """Queue a message about `actor_email`'s action, unless it is the operator's."""
        if self.send is None or actor_email.lower() == self.operator:
            return
        text = "\n".join([f"👤 {actor_email}", *lines])
        background.add_task(self._send, self.send, TelegramMessage(self.chat_id, text, False))

    async def tell_about_user(
        self, background: BackgroundTasks, session: AsyncSession, user_id: int, lines: list[str]
    ) -> None:
        if self.send is None:
            return
        email = (await session.execute(select(User.email).where(User.id == user_id))).scalar_one()
        self.tell(background, email, lines)

    @staticmethod
    async def _send(send: SendTelegram, msg: TelegramMessage) -> None:
        try:
            await send(msg)
        except Exception as exc:
            log.warning("operator notification lost: %s: %s", type(exc).__name__, exc)


def one_line(value: str) -> str:
    """Names are user input: no line breaks that could fake extra lines in the message."""
    return " ".join(value.split())


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier


OperatorNotifier = Annotated[Notifier, Depends(get_notifier)]
