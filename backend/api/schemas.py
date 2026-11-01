"""Request and response bodies. Limits mirror the schema's CHECK constraints, so invalid
input is a 422 here and never reaches the database."""

from datetime import datetime
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from core.models import ErrorKind, IncidentEndReason, MonitorState

MAX_TIMEOUT_S = 30  # bounds the time to confirm DOWN (design/state-machine.md)
MAX_ALERT_EMAILS = 20
MAX_PAGE_ITEMS = 100

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
    in_maintenance: bool  # an active window targets the check or its group


class Patch(BaseModel):
    """A partial update: a field left out is kept. Null is accepted only by the fields in
    `clearable`, where it clears the value."""

    model_config = ConfigDict(extra="forbid")
    clearable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for field in sorted(self.model_fields_set - self.clearable):
            if getattr(self, field) is None:
                raise ValueError(f"{field} can't be null")
        return self


# ---------- Groups ----------

# Tiered over the members that count: paused checks and checks in maintenance don't.
Status = Literal["operational", "partial_outage", "major_outage"]


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    alert_emails: AlertEmails  # replaces the members' own lists


class GroupPatch(Patch):
    name: Name | None = None
    alert_emails: AlertEmails | None = None


class GroupOut(BaseModel):
    id: int
    name: str
    alert_emails: list[str]
    created_at: datetime
    status: Status
    check_count: int  # live members, paused ones included
    down_count: int  # members that count and are down


# ---------- Maintenance windows ----------

Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class MaintenanceIn(BaseModel):
    """A `starts_at` in the past means "starts now"."""

    model_config = ConfigDict(extra="forbid")

    check_id: int | None = None
    group_id: int | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    reason: Reason | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.check_id is None) == (self.group_id is None):
            raise ValueError("set exactly one of check_id and group_id")
        return self


class MaintenancePatch(Patch):
    """`starts_at` only before the window starts. An `ends_at` in the past ends an active
    window now."""

    clearable = frozenset({"reason"})

    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    reason: Reason | None = None


WindowStatus = Literal["active", "upcoming", "past"]


class MaintenanceOut(BaseModel):
    id: int
    check_id: int | None
    group_id: int | None
    starts_at: datetime
    ends_at: datetime
    reason: str | None
    created_at: datetime
    status: WindowStatus


# ---------- History ----------


class Page[T](BaseModel):
    """Cursor pagination: pass `next_cursor` back as `cursor`; null on the last page."""

    items: list[T]
    next_cursor: str | None


class Bucket(BaseModel):
    """One chart slot. Every field but `t` is null for a slot with no runs (a gap)."""

    t: datetime  # slot start
    total: int | None = None
    ok: int | None = None
    avg_ms: int | None = None
    max_ms: int | None = None


class Stats(BaseModel):
    uptime_pct: float | None = None  # over the range; null with no runs
    buckets: list[Bucket]


class ResultOut(BaseModel):
    checked_at: datetime
    ok: bool
    response_ms: int | None
    status_code: int | None
    error_kind: ErrorKind | None
    error_message: str | None
    is_retry: bool  # a confirmation retry after a failure: not counted in stats


class IncidentOut(BaseModel):
    id: int
    check_id: int
    started_at: datetime  # first failure of the streak
    detected_at: datetime  # DOWN confirmed
    ended_at: datetime | None
    duration_s: int | None  # null while open
    end_reason: IncidentEndReason | None
    cause_kind: ErrorKind | None
    cause_message: str | None
    began_in_maintenance: bool
    alerted: bool  # a DOWN email was queued


# ---------- Status pages ----------

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class StatusPageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    show_response_times: bool = False
    show_uptime: bool = True
    show_incidents: bool = False


class StatusPagePatch(Patch):
    title: Title | None = None
    is_enabled: bool | None = None
    show_response_times: bool | None = None
    show_uptime: bool | None = None
    show_incidents: bool | None = None


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: int
    display_name: Name  # shown publicly instead of the URL
    position: Annotated[int, Field(ge=0, le=32767)] = 0  # sort order; ties by id


class ItemOut(BaseModel):
    id: int
    check_id: int
    display_name: str
    position: int


class StatusPageOut(BaseModel):
    id: int
    slug: str
    title: str
    is_enabled: bool
    show_response_times: bool
    show_uptime: bool
    show_incidents: bool
    created_at: datetime
    items: list[ItemOut]  # live checks only


# ---------- Public ----------
# Separate schemas: never a URL, a check id or an error message. Optional fields are left
# out unless the page's flags allow them.

PublicState = Literal["up", "down", "pending", "paused", "maintenance"]


class PublicItem(BaseModel):
    id: int  # the item's id, used by `/public/{slug}/stats?item=`
    display_name: str
    state: PublicState
    since: datetime
    uptime_pct: float | None = None  # last 30 days, if `show_uptime`
    last_response_ms: int | None = None  # if `show_response_times`


class PublicIncident(BaseModel):
    item_id: int
    display_name: str
    started_at: datetime
    ended_at: datetime | None


class PublicPage(BaseModel):
    title: str
    overall_status: Status
    items: list[PublicItem]
    incidents: list[PublicIncident] | None = None  # last 30 days, if `show_incidents`


class Health(BaseModel):
    db: Literal["ok", "error"]
    worker_last_seen_s: int | None  # seconds since the freshest worker heartbeat
