"""Initial schema, hand-written from design/schema.sql.

Revision ID: 0001
Revises:
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One statement per entry: asyncpg prepares each statement, so it can't run a multi-statement
# string.
UPGRADE = (
    # ---------- Enums ----------
    "CREATE TYPE monitor_state AS ENUM ('pending', 'up', 'failing', 'down')",
    """
    CREATE TYPE error_kind AS ENUM (
        'timeout', 'dns', 'connect', 'tls',
        'status_mismatch', 'body_mismatch', 'too_large', 'other'
    )
    """,
    "CREATE TYPE alert_kind    AS ENUM ('down', 'up')",
    "CREATE TYPE outbox_status AS ENUM ('pending', 'sent', 'failed')",
    "CREATE TYPE incident_end_reason AS ENUM ('recovered', 'paused', 'check_deleted')",
    # ---------- Users ----------
    """
    CREATE TABLE users (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        email         text        NOT NULL,
        password_hash text        NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX users_email_uq ON users (lower(email))",
    # ---------- Groups ----------
    """
    CREATE TABLE check_groups (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id   bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name       text        NOT NULL,
        alert_emails text[]    NOT NULL CHECK (cardinality(alert_emails) >= 1),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (owner_id, name),
        UNIQUE (id, owner_id)
    )
    """,
    # ---------- Checks: configuration ----------
    """
    CREATE TABLE checks (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id        bigint       NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        group_id        bigint,
        name            text         NOT NULL,
        url             text         NOT NULL CHECK (url ~* '^https?://'),
        interval_s      integer      NOT NULL CHECK (interval_s BETWEEN 30 AND 3600),
        timeout_s       numeric(5,2) NOT NULL CHECK (timeout_s > 0),
        expected_status smallint     NOT NULL DEFAULT 200
                                     CHECK (expected_status BETWEEN 100 AND 599),
        expected_body   text,
        alert_emails    text[]       NOT NULL CHECK (cardinality(alert_emails) >= 1),
        is_paused       boolean      NOT NULL DEFAULT false,
        created_at      timestamptz  NOT NULL DEFAULT now(),
        updated_at      timestamptz  NOT NULL DEFAULT now(),
        deleted_at      timestamptz,
        CHECK (timeout_s < interval_s),
        UNIQUE (id, owner_id),
        FOREIGN KEY (group_id, owner_id)
            REFERENCES check_groups (id, owner_id) ON DELETE SET NULL (group_id)
    )
    """,
    "CREATE INDEX checks_owner_idx ON checks (owner_id) WHERE deleted_at IS NULL",
    "CREATE INDEX checks_group_idx ON checks (group_id) WHERE deleted_at IS NULL",
    # ---------- Checks: runtime ----------
    """
    CREATE TABLE check_state (
        check_id             bigint PRIMARY KEY REFERENCES checks (id) ON DELETE CASCADE,
        next_run_at          timestamptz NOT NULL DEFAULT now(),
        lease_owner          text,
        lease_until          timestamptz,
        state                monitor_state NOT NULL DEFAULT 'pending',
        consecutive_failures smallint      NOT NULL DEFAULT 0,
        first_failure_at     timestamptz,
        state_changed_at     timestamptz   NOT NULL DEFAULT now(),
        last_checked_at      timestamptz,
        last_ok              boolean,
        last_status_code     smallint,
        last_response_ms     integer,
        last_error_kind      error_kind,
        last_error_message   text
    ) WITH (fillfactor = 70)
    """,
    "CREATE INDEX check_state_due_idx ON check_state (next_run_at)",
    # ---------- Raw results (partitioned by day) ----------
    """
    CREATE TABLE check_results (
        check_id      bigint      NOT NULL,
        checked_at    timestamptz NOT NULL,
        ok            boolean     NOT NULL,
        response_ms   integer,
        status_code   smallint,
        error_kind    error_kind,
        error_message text,
        is_retry      boolean     NOT NULL DEFAULT false,
        PRIMARY KEY (check_id, checked_at) INCLUDE (ok, response_ms)
    ) PARTITION BY RANGE (checked_at)
    """,
    "CREATE TABLE check_results_default PARTITION OF check_results DEFAULT",
    # ---------- Hourly rollups ----------
    """
    CREATE TABLE check_stats_hourly (
        check_id  bigint      NOT NULL REFERENCES checks (id) ON DELETE CASCADE,
        bucket    timestamptz NOT NULL,
        total     integer     NOT NULL,
        ok        integer     NOT NULL,
        rt_count  integer     NOT NULL,
        rt_sum_ms bigint      NOT NULL,
        rt_min_ms integer,
        rt_max_ms integer,
        PRIMARY KEY (check_id, bucket)
    )
    """,
    # ---------- Incidents ----------
    """
    CREATE TABLE incidents (
        id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        check_id             bigint      NOT NULL REFERENCES checks (id) ON DELETE CASCADE,
        started_at           timestamptz NOT NULL,
        detected_at          timestamptz NOT NULL,
        ended_at             timestamptz,
        end_reason           incident_end_reason,
        cause_kind           error_kind,
        cause_message        text,
        began_in_maintenance boolean     NOT NULL DEFAULT false,
        down_alert_queued    boolean     NOT NULL DEFAULT false,
        CHECK (ended_at IS NULL OR ended_at >= started_at),
        CHECK ((ended_at IS NULL) = (end_reason IS NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX incidents_one_open_per_check
        ON incidents (check_id) WHERE ended_at IS NULL
    """,
    """
    CREATE INDEX incidents_check_started_idx
        ON incidents (check_id, started_at DESC)
    """,
    """
    CREATE INDEX incidents_open_unalerted_idx
        ON incidents (check_id) WHERE ended_at IS NULL AND NOT down_alert_queued
    """,
    # ---------- Maintenance windows ----------
    """
    CREATE TABLE maintenance_windows (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id   bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        check_id   bigint,
        group_id   bigint,
        starts_at  timestamptz NOT NULL,
        ends_at    timestamptz NOT NULL,
        reason     text,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (num_nonnulls(check_id, group_id) = 1),
        CHECK (ends_at > starts_at),
        FOREIGN KEY (check_id, owner_id) REFERENCES checks (id, owner_id) ON DELETE CASCADE,
        FOREIGN KEY (group_id, owner_id) REFERENCES check_groups (id, owner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX mw_check_idx ON maintenance_windows (check_id, ends_at)
        WHERE check_id IS NOT NULL
    """,
    """
    CREATE INDEX mw_group_idx ON maintenance_windows (group_id, ends_at)
        WHERE group_id IS NOT NULL
    """,
    # ---------- Notification outbox ----------
    """
    CREATE TABLE notifications (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        incident_id     bigint        NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
        kind            alert_kind    NOT NULL,
        recipient       text          NOT NULL,
        status          outbox_status NOT NULL DEFAULT 'pending',
        attempts        smallint      NOT NULL DEFAULT 0,
        next_attempt_at timestamptz   NOT NULL DEFAULT now(),
        last_error      text,
        created_at      timestamptz   NOT NULL DEFAULT now(),
        sent_at         timestamptz,
        UNIQUE (incident_id, kind, recipient)
    )
    """,
    """
    CREATE INDEX notifications_due_idx
        ON notifications (next_attempt_at) WHERE status = 'pending'
    """,
    # ---------- Public status pages ----------
    """
    CREATE TABLE status_pages (
        id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id            bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        slug                text        NOT NULL UNIQUE,
        title               text        NOT NULL,
        is_enabled          boolean     NOT NULL DEFAULT true,
        show_response_times boolean     NOT NULL DEFAULT false,
        show_uptime         boolean     NOT NULL DEFAULT true,
        show_incidents      boolean     NOT NULL DEFAULT false,
        created_at          timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, owner_id)
    )
    """,
    """
    CREATE TABLE status_page_items (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id       bigint   NOT NULL,
        status_page_id bigint   NOT NULL,
        check_id       bigint   NOT NULL,
        display_name   text     NOT NULL,
        position       smallint NOT NULL DEFAULT 0,
        UNIQUE (status_page_id, check_id),
        FOREIGN KEY (status_page_id, owner_id)
            REFERENCES status_pages (id, owner_id) ON DELETE CASCADE,
        FOREIGN KEY (check_id, owner_id)
            REFERENCES checks (id, owner_id) ON DELETE CASCADE
    )
    """,
    # ---------- Worker heartbeats ----------
    """
    CREATE TABLE worker_heartbeats (
        worker_id    text PRIMARY KEY,
        started_at   timestamptz NOT NULL,
        last_seen_at timestamptz NOT NULL
    )
    """,
)

# Dropping check_results also drops all of its partitions.
DOWNGRADE = (
    """
    DROP TABLE
        worker_heartbeats, status_page_items, status_pages, notifications,
        maintenance_windows, incidents, check_stats_hourly, check_results,
        check_state, checks, check_groups, users
    """,
    "DROP TYPE incident_end_reason, outbox_status, alert_kind, error_kind, monitor_state",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
