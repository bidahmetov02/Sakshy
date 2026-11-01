"""Request and response bodies. Limits mirror the schema's CHECK constraints, so invalid
input is a 422 here and never reaches the database."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from core.models import ErrorKind, MonitorState

MAX_TIMEOUT_S = 30  # bounds the time to confirm DOWN (design/state-machine.md)
MAX_ALERT_EMAILS = 20

_http_url = TypeAdapter(AnyHttpUrl)


def _check_url(url: str) -> str:
    """Validated as an http(s) URL with a host, but stored exactly as typed."""
    try:
        _http_url.validate_python(url)
    except ValidationError:
        raise ValueError("must be an http:// or https:// URL") from None
    return url


def _dedupe(emails: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for email in emails:
        if email.lower() not in seen:
            seen.add(email.lower())
            unique.append(email)
    return unique


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Url = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=2048),
    AfterValidator(_check_url),
]
Interval = Annotated[int, Field(ge=30, le=3600)]
Timeout = Annotated[float, Field(ge=0.01, le=MAX_TIMEOUT_S), AfterValidator(lambda t: round(t, 2))]
ExpectedStatus = Annotated[int, Field(ge=100, le=599)]
ExpectedBody = Annotated[str, Field(max_length=1000), AfterValidator(lambda b: b or None)]
AlertEmails = Annotated[
    list[EmailStr], Field(min_length=1, max_length=MAX_ALERT_EMAILS), AfterValidator(_dedupe)
]


class Credentials(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]


class UserOut(BaseModel):
    id: int
    email: str


class CheckIn(BaseModel):
    """A check's whole configuration: the POST body, and a PATCH merged onto the stored
    config (so cross-field rules hold after a partial update too)."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    url: Url
    interval_s: Interval
    timeout_s: Timeout
    expected_status: ExpectedStatus = 200
    expected_body: ExpectedBody | None = None
    group_id: int | None = None
    alert_emails: AlertEmails  # used only while ungrouped: a group's list replaces it

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> Self:
        if self.timeout_s >= self.interval_s:
            raise ValueError("timeout_s must be less than interval_s")
        return self


class CheckPatch(BaseModel):
    """Any subset of the config. An explicit null clears `group_id` or `expected_body`."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    url: Url | None = None
    interval_s: Interval | None = None
    timeout_s: Timeout | None = None
    expected_status: ExpectedStatus | None = None
    expected_body: ExpectedBody | None = None
    group_id: int | None = None
    alert_emails: AlertEmails | None = None


class CheckOut(BaseModel):
    """Config plus live state: the dashboard snapshot that SSE events then patch."""

    id: int
    name: str
    url: str
    interval_s: int
    timeout_s: float
    expected_status: int
    expected_body: str | None
    group_id: int | None
    alert_emails: list[str]
    effective_alert_emails: list[str]  # who actually gets alerted: the group's list if grouped
    is_paused: bool
    created_at: datetime
    updated_at: datetime

    state: MonitorState
    state_changed_at: datetime
    next_run_at: datetime
    last_checked_at: datetime | None
    last_ok: bool | None
    last_status_code: int | None
    last_response_ms: int | None
    last_error_kind: ErrorKind | None
    last_error_message: str | None
    incident_started_at: datetime | None  # the open incident, if any


class Health(BaseModel):
    db: Literal["ok", "error"]
    worker_last_seen_s: int | None  # seconds since the freshest worker heartbeat
