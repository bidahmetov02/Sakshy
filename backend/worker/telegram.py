"""Telegram alert content: to the operator's chat, for every check on the server.

The operator's own checks ring and link to the check; other users' checks arrive silently,
headed by their owner, with no link (the operator can't open them). They go through the
outbox like emails, as rows whose recipient is `telegram:<chat id>`.
"""

from core.models import AlertKind
from core.settings import Settings
from core.telegram import TelegramMessage
from worker.emails import Alert, cause, end_note, format_duration, one_line, timestamp

PREFIX = "telegram:"


def recipient(settings: Settings) -> str | None:
    """The outbox recipient for the configured chat, if Telegram alerts are on."""
    return f"{PREFIX}{settings.telegram_chat_id}" if settings.telegram_enabled else None


def render(alert: Alert, operator_email: str, app_base_url: str) -> TelegramMessage:
    mine = alert.owner_email.lower() == operator_email.lower()
    name = one_line(alert.check_name)
    match alert.kind:
        case AlertKind.DOWN:
            lines = [
                f"🔴 DOWN: {name}",
                alert.url,
                f"Cause: {cause(alert)}",
                f"Down since: {timestamp(alert.started_at)}",
            ]
            if alert.ended_at is not None:
                lines += [end_note(alert.ended_at, alert.end_reason)]
        case AlertKind.UP:
            if alert.ended_at is None:
                raise ValueError(f"UP alert {alert.notification_id} for an open incident")
            downtime = format_duration(alert.ended_at - alert.started_at)
            lines = [
                f"🟢 UP: {name} (down {downtime})",
                alert.url,
                f"Recovered: {timestamp(alert.ended_at)}",
            ]
    if mine:
        lines += [f"→ {app_base_url.rstrip('/')}/checks/{alert.check_id}"]
    else:
        lines = [f"👤 {alert.owner_email}", *lines]
    return TelegramMessage(
        chat_id=alert.recipient.removeprefix(PREFIX), text="\n".join(lines), silent=not mine
    )
