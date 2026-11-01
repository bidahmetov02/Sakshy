"""Alert email content: pure functions, no I/O. Plain text, times in UTC (users have no
stored timezone). One email per recipient, so recipients never see each other."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, parseaddr

from core.models import AlertKind, ErrorKind, IncidentEndReason


@dataclass(frozen=True)
class Alert:
    """One claimed outbox row with what its email needs."""

    notification_id: int
    attempts: int  # including the current one
    kind: AlertKind
    recipient: str
    check_id: int
    check_name: str
    url: str
    started_at: datetime
    detected_at: datetime
    ended_at: datetime | None
    end_reason: IncidentEndReason | None
    cause_kind: ErrorKind | None
    cause_message: str | None


def render(alert: Alert, mail_from: str, app_base_url: str) -> EmailMessage:
    name = _one_line(alert.check_name)
    match alert.kind:
        case AlertKind.DOWN:
            subject, lines = f"[Sakshy] DOWN: {name}", _down_lines(alert, name)
        case AlertKind.UP:
            if alert.ended_at is None:
                raise ValueError(f"UP alert {alert.notification_id} for an open incident")
            downtime = format_duration(alert.ended_at - alert.started_at)
            subject = f"[Sakshy] UP: {name} (down {downtime})"
            lines = [
                f"{name} is UP again.",
                "",
                f"URL:        {alert.url}",
                f"Down since: {_ts(alert.started_at)}",
                f"Recovered:  {_ts(alert.ended_at)}",
                f"Downtime:   {downtime}",
            ]
    lines += ["", f"View the check: {app_base_url.rstrip('/')}/checks/{alert.check_id}"]

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = alert.recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(usegmt=True)
    # Stable across retries, so a re-sent copy can be recognised as the same email.
    domain = parseaddr(mail_from)[1].rpartition("@")[2] or "sakshy.local"
    msg["Message-ID"] = f"<notification-{alert.notification_id}@{domain}>"
    msg.set_content("\n".join(lines) + "\n")
    return msg


def _down_lines(alert: Alert, name: str) -> list[str]:
    cause = ": ".join(str(part) for part in (alert.cause_kind, alert.cause_message) if part)
    lines = [
        f"{name} is DOWN.",
        "",
        f"URL:        {alert.url}",
        f"Cause:      {cause or 'unknown'}",
        f"Down since: {_ts(alert.started_at)}",
        f"Detected:   {_ts(alert.detected_at)}",
    ]
    if alert.ended_at is not None:  # the email was delayed (SMTP retries) past the incident
        reason = alert.end_reason.value.replace("_", " ") if alert.end_reason else "ended"
        lines += [
            "",
            f"This alert was delayed: the incident ended at {_ts(alert.ended_at)} ({reason}).",
        ]
    return lines


def format_duration(delta: timedelta) -> str:
    """The two most significant units: '40s', '12m 5s', '1h 5m', '2d 3h'."""
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")]
    first = next((i for i, (value, _) in enumerate(parts) if value), len(parts) - 1)
    return " ".join(f"{value}{unit}" for value, unit in parts[first : first + 2] if value) or "0s"


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _one_line(value: str) -> str:
    """Check names are user input: no line breaks in a header."""
    return " ".join(value.split())
