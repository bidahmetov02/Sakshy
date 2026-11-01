"""Alert email content (pure, no I/O)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from core.models import AlertKind, ErrorKind, IncidentEndReason
from worker.emails import Alert, format_duration, render

T = datetime(2026, 10, 30, 14, 20, 5, tzinfo=UTC)
FROM = "Sakshy <alerts@sakshy.local>"
BASE = "http://localhost:8080/"

DOWN = Alert(
    notification_id=7,
    attempts=1,
    kind=AlertKind.DOWN,
    recipient="ops@x.io",
    check_id=17,
    check_name="Shop",
    url="https://shop.example",
    started_at=T,
    detected_at=T + timedelta(seconds=20),
    ended_at=None,
    end_reason=None,
    cause_kind=ErrorKind.TIMEOUT,
    cause_message="no response within 10 s",
)
UP = replace(
    DOWN,
    kind=AlertKind.UP,
    ended_at=T + timedelta(minutes=12, seconds=5),
    end_reason=IncidentEndReason.RECOVERED,
)


def test_down_email() -> None:
    msg = render(DOWN, FROM, BASE)

    assert msg["Subject"] == "[Sakshy] DOWN: Shop"
    assert (msg["From"], msg["To"]) == (FROM, "ops@x.io")
    assert msg["Message-ID"] == "<notification-7@sakshy.local>"
    assert msg["Date"]
    body = msg.get_content()
    assert "Shop is DOWN." in body
    assert "URL:        https://shop.example" in body
    assert "Cause:      timeout: no response within 10 s" in body
    assert "Down since: 2026-10-30 14:20:05 UTC" in body
    assert "Detected:   2026-10-30 14:20:25 UTC" in body
    assert "View the check: http://localhost:8080/checks/17" in body
    assert "delayed" not in body


def test_up_email_reports_the_downtime() -> None:
    msg = render(UP, FROM, BASE)

    assert msg["Subject"] == "[Sakshy] UP: Shop (down 12m 5s)"
    body = msg.get_content()
    assert "Shop is UP again." in body
    assert "Down since: 2026-10-30 14:20:05 UTC" in body
    assert "Recovered:  2026-10-30 14:32:10 UTC" in body
    assert "Downtime:   12m 5s" in body


def test_up_email_needs_an_ended_incident() -> None:
    with pytest.raises(ValueError, match="open incident"):
        render(replace(UP, ended_at=None), FROM, BASE)


def test_a_delayed_down_email_says_the_incident_already_ended() -> None:
    late = replace(DOWN, ended_at=T + timedelta(minutes=5), end_reason=IncidentEndReason.PAUSED)
    body = render(late, FROM, BASE).get_content()
    assert "the incident ended at 2026-10-30 14:25:05 UTC (paused)" in body


@pytest.mark.parametrize(
    ("kind", "message", "shown"),
    [
        (ErrorKind.STATUS_MISMATCH, None, "status_mismatch"),
        (None, None, "unknown"),
    ],
)
def test_cause_falls_back_to_the_error_kind(
    kind: ErrorKind | None, message: str | None, shown: str
) -> None:
    body = render(replace(DOWN, cause_kind=kind, cause_message=message), FROM, BASE).get_content()
    assert f"Cause:      {shown}\n" in body


def test_check_name_cannot_inject_headers() -> None:
    msg = render(replace(DOWN, check_name="Shop\r\nBcc: evil@x.io"), FROM, BASE)
    assert msg["Subject"] == "[Sakshy] DOWN: Shop Bcc: evil@x.io"
    assert msg["Bcc"] is None


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-5, "0s"),
        (0, "0s"),
        (40, "40s"),
        (60, "1m"),
        (725, "12m 5s"),
        (3600, "1h"),
        (3600 + 5 * 60 + 9, "1h 5m"),
        (2 * 86_400 + 3 * 3600 + 59, "2d 3h"),
    ],
)
def test_duration_shows_the_two_largest_units(seconds: int, text: str) -> None:
    assert format_duration(timedelta(seconds=seconds)) == text
