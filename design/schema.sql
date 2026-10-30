-- =====================================================================
-- Uptime monitor: database schema draft (PostgreSQL 18)
--
-- Conventions
--   * All timestamps are timestamptz (stored UTC, converted in the browser).
--   * Ids are bigint identity.
--   * Scheduling and lease comparisons use the DATABASE clock (now()),
--     never the worker's clock, so N workers can never disagree about time.
--   * Tenant integrity: owned tables carry owner_id, and cross-table links
--     use composite FKs (x_id, owner_id) so the DB itself refuses e.g.
--     putting user A's check into user B's group.
-- =====================================================================


-- ---------- Enums ----------------------------------------------------
CREATE TYPE monitor_state AS ENUM ('pending', 'up', 'failing', 'down');
-- pending = no result yet; failing = failed, threshold not reached yet
-- (paused / maintenance are display overlays, not stored states)

CREATE TYPE error_kind AS ENUM (
    'timeout', 'dns', 'connect', 'tls',
    'status_mismatch', 'body_mismatch', 'too_large', 'other'
);

CREATE TYPE alert_kind    AS ENUM ('down', 'up');
CREATE TYPE outbox_status AS ENUM ('pending', 'sent', 'failed');

-- Why an incident ended. Only 'recovered' can produce an UP email.
CREATE TYPE incident_end_reason AS ENUM ('recovered', 'paused', 'check_deleted');


-- ---------- Users ----------------------------------------------------
CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         text        NOT NULL,
    password_hash text        NOT NULL,           -- argon2
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_uq ON users (lower(email));


-- ---------- Groups ---------------------------------------------------
CREATE TABLE check_groups (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id   bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name       text        NOT NULL,
    -- Required, pre-filled with the account email by the UI.
    -- Alert routing: a grouped check alerts the GROUP's list, which replaces
    -- the check's own list. An ungrouped check alerts its own list.
    alert_emails text[]    NOT NULL CHECK (cardinality(alert_emails) >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name),
    UNIQUE (id, owner_id)                         -- target for composite FKs
);


-- ---------- Checks: CONFIGURATION (user-owned, rarely changes) ------
CREATE TABLE checks (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id        bigint       NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    group_id        bigint,                       -- one group per check (or none)
    name            text         NOT NULL,
    url             text         NOT NULL CHECK (url ~* '^https?://'),
    interval_s      integer      NOT NULL CHECK (interval_s BETWEEN 30 AND 3600),
    timeout_s       numeric(5,2) NOT NULL CHECK (timeout_s > 0),
    expected_status smallint     NOT NULL DEFAULT 200
                                 CHECK (expected_status BETWEEN 100 AND 599),
    expected_body   text,                         -- optional substring
    alert_emails    text[]       NOT NULL CHECK (cardinality(alert_emails) >= 1),  -- used only while ungrouped
    is_paused      boolean      NOT NULL DEFAULT false,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    deleted_at      timestamptz,                  -- soft delete
    CHECK (timeout_s < interval_s),
    UNIQUE (id, owner_id),
    -- Group deleted -> check becomes ungrouped. Only group_id is nulled
    -- (PG15+ column list), owner_id stays.
    FOREIGN KEY (group_id, owner_id)
        REFERENCES check_groups (id, owner_id) ON DELETE SET NULL (group_id)
);
CREATE INDEX checks_owner_idx ON checks (owner_id) WHERE deleted_at IS NULL;
CREATE INDEX checks_group_idx ON checks (group_id) WHERE deleted_at IS NULL;


-- ---------- Checks: RUNTIME (worker-owned, updated every run) -------
-- Split from `checks` so the hot, constantly-updated row is separate from
-- the config the API edits: less contention, clear ownership.
-- fillfactor 70 leaves room on each page for in-place (HOT) updates.
CREATE TABLE check_state (
    check_id             bigint PRIMARY KEY REFERENCES checks (id) ON DELETE CASCADE,

    -- scheduling + lease
    next_run_at          timestamptz NOT NULL DEFAULT now(),
    lease_owner          text,                    -- worker id
    lease_until          timestamptz,             -- lease expiry (crash safety)

    -- status
    state                monitor_state NOT NULL DEFAULT 'pending',
    consecutive_failures smallint      NOT NULL DEFAULT 0,
    first_failure_at     timestamptz,             -- start of current failure streak
    state_changed_at     timestamptz   NOT NULL DEFAULT now(),

    -- last result (dashboard reads this, never the results table)
    last_checked_at      timestamptz,
    last_ok              boolean,
    last_status_code     smallint,
    last_response_ms     integer,
    last_error_kind      error_kind,
    last_error_message   text
) WITH (fillfactor = 70);
CREATE INDEX check_state_due_idx ON check_state (next_run_at);


-- ---------- Raw results (time series, partitioned by day) ----------
-- No FK to checks: retention = DROP old partitions, deletion = soft delete;
-- a FK would force slow cascading deletes across every partition.
-- PK doubles as the query index; INCLUDE makes day-view reads index-only.
CREATE TABLE check_results (
    check_id      bigint      NOT NULL,
    checked_at    timestamptz NOT NULL,
    ok            boolean     NOT NULL,
    response_ms   integer,                        -- NULL if no response
    status_code   smallint,
    error_kind    error_kind,
    error_message text,                           -- truncated by the app
    is_retry      boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (check_id, checked_at) INCLUDE (ok, response_ms)
) PARTITION BY RANGE (checked_at);

-- Safety net so an insert never fails for lack of a partition.
-- Caveat: if rows land here, creating that day's partition later fails
-- until they are moved, so the worker must create partitions days ahead.
CREATE TABLE check_results_default PARTITION OF check_results DEFAULT;

-- Created by the worker's housekeeping job, ~7 days ahead, e.g.:
-- CREATE TABLE check_results_20261030 PARTITION OF check_results
--     FOR VALUES FROM ('2026-10-30 00:00+00') TO ('2026-10-31 00:00+00');
-- Retention: DROP TABLE check_results_20261001;   -- instant


-- ---------- Hourly rollups (week / month charts, uptime %) ----------
-- Updated in the same transaction as each raw insert (write-time rollup).
-- Retries are NOT counted, so totals stay comparable across checks.
CREATE TABLE check_stats_hourly (
    check_id  bigint      NOT NULL REFERENCES checks (id) ON DELETE CASCADE,
    bucket    timestamptz NOT NULL,               -- date_trunc('hour', checked_at)
    total     integer     NOT NULL,
    ok        integer     NOT NULL,
    rt_count  integer     NOT NULL,               -- results that HAD a response time
    rt_sum_ms bigint      NOT NULL,
    rt_min_ms integer,
    rt_max_ms integer,
    PRIMARY KEY (check_id, bucket)
);


-- ---------- Incidents -----------------------------------------------
CREATE TABLE incidents (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_id             bigint      NOT NULL REFERENCES checks (id) ON DELETE CASCADE,
    started_at           timestamptz NOT NULL,    -- first failure of the streak (backdated)
    detected_at          timestamptz NOT NULL,    -- threshold crossed
    ended_at             timestamptz,             -- NULL = ongoing
    end_reason           incident_end_reason,     -- recovered / paused / check_deleted
    cause_kind           error_kind,
    cause_message        text,
    began_in_maintenance boolean     NOT NULL DEFAULT false,
    -- Email rule: an UP email is sent  <=>  end_reason = 'recovered'
    --                                  AND down_alert_queued = true,
    -- whether or not a maintenance window is active at recovery time.
    down_alert_queued    boolean     NOT NULL DEFAULT false,  -- false = suppressed so far
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);
-- The DB guarantees at most ONE open incident per check.
CREATE UNIQUE INDEX incidents_one_open_per_check
    ON incidents (check_id) WHERE ended_at IS NULL;
CREATE INDEX incidents_check_started_idx
    ON incidents (check_id, started_at DESC);
-- Used by the "maintenance ended, still down" sweep.
CREATE INDEX incidents_open_unalerted_idx
    ON incidents (check_id) WHERE ended_at IS NULL AND NOT down_alert_queued;


-- ---------- Maintenance windows (one-off) ---------------------------
-- Targets exactly one check OR one group. Group windows apply to the
-- group's members as of evaluation time.
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
);
CREATE INDEX mw_check_idx ON maintenance_windows (check_id, ends_at) WHERE check_id IS NOT NULL;
CREATE INDEX mw_group_idx ON maintenance_windows (group_id, ends_at) WHERE group_id IS NOT NULL;


-- ---------- Notification outbox -------------------------------------
-- Rows are inserted in the SAME transaction as the incident change.
-- Recipients are resolved (snapshotted) at enqueue time.
-- UNIQUE makes a duplicate *row* impossible. A crash between SMTP success
-- and marking 'sent' can still re-send once (at-least-once delivery).
-- Claiming reuses next_attempt_at as a lease: the sender sets it to
-- now() + 60 s when it picks a row, so a crashed sender's rows retry later.
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
);
CREATE INDEX notifications_due_idx
    ON notifications (next_attempt_at) WHERE status = 'pending';


-- ---------- Public status pages -------------------------------------
CREATE TABLE status_pages (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id            bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    slug                text        NOT NULL UNIQUE,   -- random/unguessable by default
    title               text        NOT NULL,
    is_enabled          boolean     NOT NULL DEFAULT true,
    show_response_times boolean     NOT NULL DEFAULT false,
    show_uptime         boolean     NOT NULL DEFAULT true,
    show_incidents      boolean     NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);

-- Allowlist: the page lists checks one by one (the page acts as its own
-- group: its overall status is aggregated over these items only).
-- URLs are never public, only display_name.
CREATE TABLE status_page_items (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id       bigint   NOT NULL,
    status_page_id bigint   NOT NULL,
    check_id       bigint   NOT NULL,
    display_name   text     NOT NULL,
    position       smallint NOT NULL DEFAULT 0,
    UNIQUE (status_page_id, check_id),
    FOREIGN KEY (status_page_id, owner_id) REFERENCES status_pages (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (check_id, owner_id)       REFERENCES checks (id, owner_id)       ON DELETE CASCADE
);


-- ---------- Worker heartbeats ---------------------------------------
-- Lets the UI mark "monitoring was offline" explicitly, and helps debugging.
CREATE TABLE worker_heartbeats (
    worker_id    text PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);


-- =====================================================================
-- KEY QUERIES (reference, not part of the migration)
-- =====================================================================

-- 1) CLAIM due checks. Every worker runs this every ~1 s.
--    SKIP LOCKED: concurrent workers get disjoint rows, never block.
--    The lease outlives the hard timeout, so a check can't overlap itself.
--
-- UPDATE check_state s
-- SET lease_owner = :worker_id,
--     lease_until = now() + make_interval(secs => c.timeout_s + 5)
-- FROM checks c
-- WHERE c.id = s.check_id
--   AND s.check_id IN (
--       SELECT s2.check_id
--       FROM check_state s2
--       JOIN checks c2 ON c2.id = s2.check_id
--       WHERE s2.next_run_at <= now()
--         AND (s2.lease_until IS NULL OR s2.lease_until < now())
--         AND NOT c2.is_paused
--         AND c2.deleted_at IS NULL
--       ORDER BY s2.next_run_at
--       LIMIT :batch
--       FOR UPDATE OF s2 SKIP LOCKED)
-- RETURNING s.*, c.url, c.timeout_s, c.interval_s,
--           c.expected_status, c.expected_body, c.group_id, c.owner_id;

-- LOCK-ORDER RULE: any transaction that changes a check's runtime state
-- (worker completion, pause/resume/delete in the API, maintenance sweep)
-- locks that check's check_state row FIRST, then incidents/outbox.
-- One order everywhere = no deadlocks, and every decision is made on
-- the latest committed state.

-- 2) COMPLETE a run: ONE transaction
--    a. SELECT s.*, c.is_paused, c.deleted_at FROM check_state s
--       JOIN checks c ON c.id = s.check_id
--       WHERE s.check_id = :id AND s.lease_owner = :worker_id
--       FOR UPDATE OF s;
--         0 rows                -> lease lost (fenced out): ROLLBACK, discard
--         paused or deleted     -> clear lease only, discard result, COMMIT
--    b. INSERT INTO check_results ...
--    c. rollup upsert (below), skipped for retries
--    d. state machine -> on transition, lock + update the open incident,
--       INSERT notifications (recipients = group's alert_emails if grouped, else the check's)
--    e. UPDATE check_state SET state, counters, last_*,
--       next_run_at = <anchored next slot or retry time, computed in Python>,
--       lease_owner = NULL, lease_until = NULL
--    f. SELECT pg_notify('check_events', '{"check_id": ..}')
--    g. COMMIT

-- 3) Rollup upsert
--    Gotcha (tested): one multi-row INSERT ... ON CONFLICT that hits the SAME
--    (check_id, bucket) twice fails with "cannot affect row a second time".
--    If you batch writes, pre-aggregate per (check_id, bucket) in Python first.
-- INSERT INTO check_stats_hourly AS h
--     (check_id, bucket, total, ok, rt_count, rt_sum_ms, rt_min_ms, rt_max_ms)
-- VALUES (:id, date_trunc('hour', :ts), 1, :ok::int,
--         (:rt IS NOT NULL)::int, coalesce(:rt, 0), :rt, :rt)
-- ON CONFLICT (check_id, bucket) DO UPDATE SET
--     total     = h.total + 1,
--     ok        = h.ok + EXCLUDED.ok,
--     rt_count  = h.rt_count + EXCLUDED.rt_count,
--     rt_sum_ms = h.rt_sum_ms + EXCLUDED.rt_sum_ms,
--     rt_min_ms = LEAST(h.rt_min_ms, EXCLUDED.rt_min_ms),
--     rt_max_ms = GREATEST(h.rt_max_ms, EXCLUDED.rt_max_ms);

-- 4) Month chart WITH GAPS: every hour gets a row; NULL total = gap.
-- SELECT b.bucket, s.total, s.ok,
--        s.rt_sum_ms / NULLIF(s.rt_count, 0) AS avg_ms, s.rt_max_ms
-- FROM generate_series(date_trunc('hour', now() - interval '30 days'),
--                      date_trunc('hour', now()), interval '1 hour') AS b(bucket)
-- LEFT JOIN check_stats_hourly s
--        ON s.check_id = :id AND s.bucket = b.bucket
-- ORDER BY b.bucket;

-- 5) Uptime % for a period (count-based)
-- SELECT sum(ok)::float8 / NULLIF(sum(total), 0)
-- FROM check_stats_hourly
-- WHERE check_id = :id AND bucket >= now() - interval '30 days';

-- 6) Is a check in maintenance right now?
-- SELECT EXISTS (
--   SELECT 1 FROM maintenance_windows w
--   WHERE now() >= w.starts_at AND now() < w.ends_at
--     AND (w.check_id = :check_id OR w.group_id = :group_id));

-- 7) Sweep: maintenance over, still down, never alerted -> enqueue DOWN email.
--    Locks check_state first (lock-order rule). SKIP LOCKED: if a check task
--    is completing that check right now, skip it this round. This prevents
--    the race "sweep queues DOWN while the task closes the incident without
--    an UP email" (which would leave the user with DOWN and no UP).
-- SELECT i.id, i.check_id
-- FROM check_state s
-- JOIN checks c    ON c.id = s.check_id
-- JOIN incidents i ON i.check_id = s.check_id AND i.ended_at IS NULL
-- WHERE NOT i.down_alert_queued
--   AND NOT EXISTS (
--       SELECT 1 FROM maintenance_windows w
--       WHERE now() >= w.starts_at AND now() < w.ends_at
--         AND (w.check_id = c.id OR w.group_id = c.group_id))
-- FOR UPDATE OF s, i SKIP LOCKED;
--    then INSERT notifications + UPDATE incidents SET down_alert_queued = true

-- 8) PAUSE (API), one transaction. Resume = is_paused false, next_run_at = now().
-- SELECT 1 FROM check_state WHERE check_id = :id FOR UPDATE;      -- lock first
-- UPDATE checks SET is_paused = true, updated_at = now() WHERE id = :id;
-- UPDATE check_state SET state = 'pending', consecutive_failures = 0,
--        first_failure_at = NULL, state_changed_at = now() WHERE check_id = :id;
-- UPDATE incidents SET ended_at = now(), end_reason = 'paused'     -- no UP email
--  WHERE check_id = :id AND ended_at IS NULL;
-- SELECT pg_notify(...); COMMIT;
--    Soft delete is the same with deleted_at = now() and end_reason = 'check_deleted'.

-- 9) RUN NOW (API): only if not currently running, else 409 Conflict.
-- UPDATE check_state SET next_run_at = now()
--  WHERE check_id = :id AND (lease_until IS NULL OR lease_until < now());
