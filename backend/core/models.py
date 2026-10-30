"""ORM mirror of the schema (`design/schema.sql`, migration 0001).

The migration is the source of truth; `tests/test_schema.py` fails if these models drift
from the migrated database. Constraint names follow PostgreSQL's own defaults, so names
here match what the hand-written SQL produced.
"""

import enum
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MonitorState(enum.StrEnum):
    PENDING = "pending"
    UP = "up"
    FAILING = "failing"
    DOWN = "down"


class ErrorKind(enum.StrEnum):
    TIMEOUT = "timeout"
    DNS = "dns"
    CONNECT = "connect"
    TLS = "tls"
    STATUS_MISMATCH = "status_mismatch"
    BODY_MISMATCH = "body_mismatch"
    TOO_LARGE = "too_large"
    OTHER = "other"


class AlertKind(enum.StrEnum):
    DOWN = "down"
    UP = "up"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class IncidentEndReason(enum.StrEnum):
    RECOVERED = "recovered"
    PAUSED = "paused"
    CHECK_DELETED = "check_deleted"


def _enum_values(enum_cls: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    # Store the lowercase values, not the member names.
    return Enum(enum_cls, name=name, values_callable=_enum_values)


NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text,
        datetime: DateTime(timezone=True),
        list[str]: ARRAY(Text),
        MonitorState: _pg_enum(MonitorState, "monitor_state"),
        ErrorKind: _pg_enum(ErrorKind, "error_kind"),
        AlertKind: _pg_enum(AlertKind, "alert_kind"),
        OutboxStatus: _pg_enum(OutboxStatus, "outbox_status"),
        IncidentEndReason: _pg_enum(IncidentEndReason, "incident_end_reason"),
    }


BigIntPK = Annotated[int, mapped_column(BigInteger, Identity(always=True), primary_key=True)]
CreatedAt = Annotated[datetime, mapped_column(server_default=func.now())]
OwnerFK = Annotated[int, mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))]
CheckFK = Annotated[int, mapped_column(BigInteger, ForeignKey("checks.id", ondelete="CASCADE"))]


class User(Base):
    __tablename__ = "users"

    id: Mapped[BigIntPK]
    email: Mapped[str]
    password_hash: Mapped[str]
    created_at: Mapped[CreatedAt]


Index("users_email_uq", func.lower(User.email), unique=True)


class CheckGroup(Base):
    __tablename__ = "check_groups"
    __table_args__ = (
        CheckConstraint("cardinality(alert_emails) >= 1", name="check_groups_alert_emails_check"),
        UniqueConstraint("owner_id", "name"),
        UniqueConstraint("id", "owner_id"),
    )

    id: Mapped[BigIntPK]
    owner_id: Mapped[OwnerFK]
    name: Mapped[str]
    alert_emails: Mapped[list[str]]
    created_at: Mapped[CreatedAt]


class Check(Base):
    __tablename__ = "checks"
    __table_args__ = (
        CheckConstraint("url ~* '^https?://'", name="checks_url_check"),
        CheckConstraint("interval_s BETWEEN 30 AND 3600", name="checks_interval_s_check"),
        CheckConstraint("timeout_s > 0", name="checks_timeout_s_check"),
        CheckConstraint("expected_status BETWEEN 100 AND 599", name="checks_expected_status_check"),
        CheckConstraint("cardinality(alert_emails) >= 1", name="checks_alert_emails_check"),
        CheckConstraint("timeout_s < interval_s", name="checks_check"),
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["group_id", "owner_id"],
            ["check_groups.id", "check_groups.owner_id"],
            ondelete="SET NULL (group_id)",
        ),
    )

    id: Mapped[BigIntPK]
    owner_id: Mapped[OwnerFK]
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str]
    url: Mapped[str]
    interval_s: Mapped[int]
    timeout_s: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    expected_status: Mapped[int] = mapped_column(SmallInteger, server_default=text("200"))
    expected_body: Mapped[str | None]
    alert_emails: Mapped[list[str]]
    is_paused: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None]


Index("checks_owner_idx", Check.owner_id, postgresql_where=text("deleted_at IS NULL"))
Index("checks_group_idx", Check.group_id, postgresql_where=text("deleted_at IS NULL"))


class CheckState(Base):
    __tablename__ = "check_state"
    __table_args__ = ({"postgresql_with": {"fillfactor": 70}},)

    check_id: Mapped[CheckFK] = mapped_column(primary_key=True)
    next_run_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_owner: Mapped[str | None]
    lease_until: Mapped[datetime | None]
    state: Mapped[MonitorState] = mapped_column(server_default=MonitorState.PENDING.value)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    first_failure_at: Mapped[datetime | None]
    state_changed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_checked_at: Mapped[datetime | None]
    last_ok: Mapped[bool | None]
    last_status_code: Mapped[int | None] = mapped_column(SmallInteger)
    last_response_ms: Mapped[int | None]
    last_error_kind: Mapped[ErrorKind | None]
    last_error_message: Mapped[str | None]


Index("check_state_due_idx", CheckState.next_run_at)


class CheckResult(Base):
    """Partitioned by day. The partitions themselves are not modelled."""

    __tablename__ = "check_results"
    __table_args__ = (
        PrimaryKeyConstraint("check_id", "checked_at", postgresql_include=["ok", "response_ms"]),
        {"postgresql_partition_by": "RANGE (checked_at)"},
    )

    check_id: Mapped[int] = mapped_column(BigInteger)
    checked_at: Mapped[datetime]
    ok: Mapped[bool]
    response_ms: Mapped[int | None]
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    error_kind: Mapped[ErrorKind | None]
    error_message: Mapped[str | None]
    is_retry: Mapped[bool] = mapped_column(server_default=false())


class CheckStatsHourly(Base):
    __tablename__ = "check_stats_hourly"

    check_id: Mapped[CheckFK] = mapped_column(primary_key=True)
    bucket: Mapped[datetime] = mapped_column(primary_key=True)
    total: Mapped[int]
    ok: Mapped[int]
    rt_count: Mapped[int]
    rt_sum_ms: Mapped[int] = mapped_column(BigInteger)
    rt_min_ms: Mapped[int | None]
    rt_max_ms: Mapped[int | None]


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="incidents_check"),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="incidents_check1"),
    )

    id: Mapped[BigIntPK]
    check_id: Mapped[CheckFK]
    started_at: Mapped[datetime]
    detected_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[IncidentEndReason | None]
    cause_kind: Mapped[ErrorKind | None]
    cause_message: Mapped[str | None]
    began_in_maintenance: Mapped[bool] = mapped_column(server_default=false())
    down_alert_queued: Mapped[bool] = mapped_column(server_default=false())


Index(
    "incidents_one_open_per_check",
    Incident.check_id,
    unique=True,
    postgresql_where=text("ended_at IS NULL"),
)
Index("incidents_check_started_idx", Incident.check_id, Incident.started_at.desc())
Index(
    "incidents_open_unalerted_idx",
    Incident.check_id,
    postgresql_where=text("ended_at IS NULL AND NOT down_alert_queued"),
)


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"
    __table_args__ = (
        CheckConstraint("num_nonnulls(check_id, group_id) = 1", name="maintenance_windows_check"),
        CheckConstraint("ends_at > starts_at", name="maintenance_windows_check1"),
        ForeignKeyConstraint(
            ["check_id", "owner_id"], ["checks.id", "checks.owner_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["group_id", "owner_id"],
            ["check_groups.id", "check_groups.owner_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[BigIntPK]
    owner_id: Mapped[OwnerFK]
    check_id: Mapped[int | None] = mapped_column(BigInteger)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    reason: Mapped[str | None]
    created_at: Mapped[CreatedAt]


Index(
    "mw_check_idx",
    MaintenanceWindow.check_id,
    MaintenanceWindow.ends_at,
    postgresql_where=text("check_id IS NOT NULL"),
)
Index(
    "mw_group_idx",
    MaintenanceWindow.group_id,
    MaintenanceWindow.ends_at,
    postgresql_where=text("group_id IS NOT NULL"),
)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("incident_id", "kind", "recipient"),)

    id: Mapped[BigIntPK]
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    kind: Mapped[AlertKind]
    recipient: Mapped[str]
    status: Mapped[OutboxStatus] = mapped_column(server_default=OutboxStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    sent_at: Mapped[datetime | None]


Index(
    "notifications_due_idx",
    Notification.next_attempt_at,
    postgresql_where=text("status = 'pending'"),
)


class StatusPage(Base):
    __tablename__ = "status_pages"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)

    id: Mapped[BigIntPK]
    owner_id: Mapped[OwnerFK]
    slug: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    is_enabled: Mapped[bool] = mapped_column(server_default=true())
    show_response_times: Mapped[bool] = mapped_column(server_default=false())
    show_uptime: Mapped[bool] = mapped_column(server_default=true())
    show_incidents: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[CreatedAt]


class StatusPageItem(Base):
    __tablename__ = "status_page_items"
    __table_args__ = (
        UniqueConstraint("status_page_id", "check_id"),
        ForeignKeyConstraint(
            ["status_page_id", "owner_id"],
            ["status_pages.id", "status_pages.owner_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["check_id", "owner_id"], ["checks.id", "checks.owner_id"], ondelete="CASCADE"
        ),
    )

    id: Mapped[BigIntPK]
    owner_id: Mapped[int] = mapped_column(BigInteger)
    status_page_id: Mapped[int] = mapped_column(BigInteger)
    check_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str]
    position: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(primary_key=True)
    started_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
