# Data model

Generated from `schema.sql`. Solid lines are real foreign keys. The **dashed**
line (`checks` to `check_results`) is a logical link with **no foreign key**,
on purpose: raw results are removed by dropping daily partitions, not by
cascading deletes.

## ERD

```mermaid
erDiagram
    users ||--o{ check_groups : owns
    users ||--o{ checks : owns
    users ||--o{ maintenance_windows : owns
    users ||--o{ status_pages : owns

    check_groups |o--o{ checks : contains
    checks ||--|| check_state : "runtime state"
    checks ||..o{ check_results : "raw results, no FK"
    checks ||--o{ check_stats_hourly : "hourly rollups"
    checks ||--o{ incidents : has
    incidents ||--o{ notifications : "outbox emails"

    checks |o--o{ maintenance_windows : "targets check"
    check_groups |o--o{ maintenance_windows : "or targets group"

    status_pages ||--o{ status_page_items : lists
    checks ||--o{ status_page_items : "shown as"

    users {
        bigint id PK
        text email UK "unique on lower(email)"
        text password_hash "argon2"
        timestamptz created_at
    }

    check_groups {
        bigint id PK
        bigint owner_id FK
        text name UK "unique per owner"
        text_array alert_emails "at least 1, replaces members' lists"
        timestamptz created_at
    }

    checks {
        bigint id PK
        bigint owner_id FK
        bigint group_id FK "nullable, one group per check"
        text name
        text url "http or https only"
        int interval_s "30 to 3600"
        numeric timeout_s "below interval_s"
        smallint expected_status "default 200"
        text expected_body "optional substring"
        text_array alert_emails "at least 1, used only while ungrouped"
        bool is_paused
        timestamptz created_at
        timestamptz updated_at
        timestamptz deleted_at "soft delete"
    }

    check_state {
        bigint check_id PK, FK
        timestamptz next_run_at "scheduler index"
        text lease_owner "worker id"
        timestamptz lease_until "lease expiry"
        monitor_state state "pending, up, failing, down"
        smallint consecutive_failures
        timestamptz first_failure_at "streak start"
        timestamptz state_changed_at
        timestamptz last_checked_at
        bool last_ok
        smallint last_status_code
        int last_response_ms
        error_kind last_error_kind
        text last_error_message
    }

    check_results {
        bigint check_id PK "logical FK"
        timestamptz checked_at PK "partition key, daily"
        bool ok
        int response_ms "null if no response"
        smallint status_code
        error_kind error_kind
        text error_message
        bool is_retry
    }

    check_stats_hourly {
        bigint check_id PK, FK
        timestamptz bucket PK "hour start"
        int total "retries excluded"
        int ok
        int rt_count "runs that had a response time"
        bigint rt_sum_ms
        int rt_min_ms
        int rt_max_ms
    }

    incidents {
        bigint id PK
        bigint check_id FK "max 1 open per check"
        timestamptz started_at "first failure, backdated"
        timestamptz detected_at "threshold crossed"
        timestamptz ended_at "null while open"
        incident_end_reason end_reason "recovered, paused, check_deleted"
        error_kind cause_kind
        text cause_message
        bool began_in_maintenance
        bool down_alert_queued "false while suppressed"
    }

    notifications {
        bigint id PK
        bigint incident_id FK
        alert_kind kind UK "down or up"
        text recipient UK "unique with incident and kind"
        outbox_status status "pending, sent, failed"
        smallint attempts
        timestamptz next_attempt_at "also the claim lease"
        text last_error
        timestamptz created_at
        timestamptz sent_at
    }

    maintenance_windows {
        bigint id PK
        bigint owner_id FK
        bigint check_id FK "exactly one of check or group"
        bigint group_id FK "exactly one of check or group"
        timestamptz starts_at
        timestamptz ends_at "after starts_at"
        text reason
        timestamptz created_at
    }

    status_pages {
        bigint id PK
        bigint owner_id FK
        text slug UK "random, unguessable"
        text title
        bool is_enabled
        bool show_response_times
        bool show_uptime
        bool show_incidents
        timestamptz created_at
    }

    status_page_items {
        bigint id PK
        bigint owner_id FK "same owner as page and check"
        bigint status_page_id FK
        bigint check_id FK "once per page"
        text display_name "shown instead of the URL"
        smallint position
    }

    worker_heartbeats {
        text worker_id PK
        timestamptz started_at
        timestamptz last_seen_at
    }
```

`worker_heartbeats` stands alone: it describes the monitor's own workers, not
user data. `text_array` in the diagram is `text[]` in SQL (Mermaid doesn't
accept brackets in type names).

## Tables at a glance

| Table | Written by | Growth | Purpose |
|---|---|---|---|
| `users` | API | per sign-up | accounts |
| `check_groups` | API | small | grouping, alert emails |
| `checks` | API | small | **configuration** the user edits |
| `check_state` | worker (+ API for pause/resume/run) | 1 row per check, updated every run | **runtime**: schedule, lease, current status. The dashboard reads only this. |
| `check_results` | worker | **2,880 rows/check/day** | raw history, partitioned by day, kept ~7 days |
| `check_stats_hourly` | worker | 720 rows/check/month | week/month charts and uptime % |
| `incidents` | worker (+ API on pause/delete) | per outage | incident log |
| `notifications` | worker | per alert email | transactional outbox |
| `maintenance_windows` | API | small | planned work |
| `status_pages`, `status_page_items` | API | small | public pages |
| `worker_heartbeats` | worker | 1 row per worker | monitor liveness |

## Enums

| Type | Values |
|---|---|
| `monitor_state` | `pending`, `up`, `failing`, `down` |
| `error_kind` | `timeout`, `dns`, `connect`, `tls`, `status_mismatch`, `body_mismatch`, `too_large`, `other` |
| `incident_end_reason` | `recovered`, `paused`, `check_deleted` |
| `alert_kind` | `down`, `up` |
| `outbox_status` | `pending`, `sent`, `failed` |

## Rules the ERD can't show

**Tenant isolation, enforced by the database:**
- Every owned table has `owner_id`. Links between owned tables are **composite
  foreign keys** on `(x_id, owner_id)`, so a row can only point at an object
  with the **same owner**. User A's check can't be put in user B's group, or
  on B's status page.
- Deleting a group sets only `checks.group_id` to null (`ON DELETE SET NULL (group_id)`), never `owner_id`.

**Uniqueness that encodes a business rule:**
- `incidents`: a partial unique index on `check_id WHERE ended_at IS NULL`, so
  **at most one open incident per check**.
- `notifications`: `UNIQUE (incident_id, kind, recipient)`, so **no duplicate
  email rows**.
- `status_page_items`: `UNIQUE (status_page_id, check_id)`.

**Check constraints:**
- `checks`: `interval_s` 30–3600, `timeout_s < interval_s`, `expected_status` 100–599, URL starts with http(s).
- `checks`, `check_groups`: `alert_emails` has at least one entry.
- `incidents`: `ended_at` and `end_reason` are both null or both set; `ended_at >= started_at`.
- `maintenance_windows`: exactly one of `check_id` / `group_id`; `ends_at > starts_at`.

**Physical design:**
- `check_results` is **partitioned by day**, with a `DEFAULT` partition as a
  safety net. Its primary key `(check_id, checked_at) INCLUDE (ok, response_ms)`
  makes day-view reads index-only.
- `check_state` uses `fillfactor = 70` to leave room on each page for frequent updates.
- Indexes: `check_state(next_run_at)` for the scheduler; partial indexes for
  due outbox rows and open, unalerted incidents; `incidents(check_id, started_at DESC)` for the log.
