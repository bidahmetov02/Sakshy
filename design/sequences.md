# Sequence diagrams

The key flows, step by step. Each one shows **who talks to whom, in which
order, and inside which transaction**.

---

## (a) One check run

```mermaid
sequenceDiagram
    autonumber
    participant CL as Worker: claim loop
    participant T as Worker: check task
    participant DB as PostgreSQL
    participant S as Target site
    participant API as API (LISTEN)

    CL->>DB: UPDATE check_state SET lease ... FOR UPDATE SKIP LOCKED
    DB-->>CL: due checks with their config
    CL->>T: spawn task (takes a semaphore slot)
    T->>S: GET url (fresh connection, hard timeout)
    S-->>T: status, body, elapsed (or timeout, DNS, TLS error)
    T->>T: evaluate → ok or error_kind
    T->>DB: BEGIN
    T->>DB: SELECT check_state + checks WHERE lease_owner = me FOR UPDATE
    alt 0 rows: lease lost (fenced out)
        T->>DB: ROLLBACK, result discarded
    else check was paused or deleted meanwhile
        T->>DB: clear lease only, discard result, COMMIT
    else normal path
        T->>T: state machine (pure function)
        T->>DB: INSERT check_results
        T->>DB: UPSERT check_stats_hourly (skipped for retries)
        opt state transition to DOWN or UP
            T->>DB: lock + INSERT or UPDATE incidents
            T->>DB: INSERT notifications (outbox)
        end
        T->>DB: UPDATE check_state (state, last_*, next_run_at, clear lease)
        T->>DB: SELECT pg_notify('check_events', ...)
        T->>DB: COMMIT
        DB-->>API: notification delivered, only because it committed
    end
    T-->>CL: semaphore slot released
```

**Why it's built this way:**
- The HTTP call happens **outside** any transaction. A 10 s timeout never holds
  a database connection or a lock.
- Everything the run changes is in **one transaction**. A crash leaves either
  all of it or none of it: never a result without a state update, or an
  incident without its email row.
- `next_run_at` is computed at completion (anchored to the schedule, or a
  retry time), so a check can never be claimed again while it's running.

---

## (b) Maintenance window ends while the site is still down

```mermaid
sequenceDiagram
    autonumber
    participant T as Worker: check task
    participant SW as Worker: maintenance sweep
    participant DB as PostgreSQL
    participant SE as Worker: outbox sender
    participant M as SMTP (Mailpit)

    Note over T,DB: Window 14:00–15:00 on group Prod
    T->>DB: 14:20 third failure → incident opened, in window, down_alert_queued = false, no outbox row
    Note over T: later failures change nothing
    Note over SW,DB: 15:00 the window ends
    SW->>DB: BEGIN
    SW->>DB: SELECT open, unalerted incidents with no active window, FOR UPDATE OF check_state, incidents SKIP LOCKED
    SW->>DB: INSERT notifications (down, recipients = group emails, or check emails if ungrouped)
    SW->>DB: UPDATE incidents SET down_alert_queued = true
    SW->>DB: COMMIT
    loop every 2 s
        SE->>DB: claim due rows: SET next_attempt_at = now() + 60 s, attempts + 1, SKIP LOCKED
        DB-->>SE: claimed rows
        SE->>M: send email
        alt sent
            SE->>DB: UPDATE status = 'sent', sent_at = now()
        else SMTP error
            SE->>DB: UPDATE next_attempt_at = now() + backoff, last_error, failed after max attempts
        end
    end
    Note over SE,DB: Sender crashes after claiming → the rows become due again after 60 s (at-least-once)
```

**Why the sweep locks `check_state` with `SKIP LOCKED`:** without it, this race
is possible. The sweep queues the DOWN email at the same moment a check task
closes the incident on recovery. The task sees `down_alert_queued = false` and
sends no UP email, so the user receives DOWN and never UP. With the lock,
whichever transaction goes second sees the other's committed result.

---

## (c) Realtime dashboard

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (React)
    participant N as nginx
    participant API as API: SSE handler
    participant L as API: listener task
    participant DB as PostgreSQL
    participant W as Worker

    L->>DB: on API startup: LISTEN check_events (dedicated connection)
    B->>N: GET /api/checks (cookie)
    N->>API: proxy
    API-->>B: snapshot: every check with its current state
    B->>N: GET /api/events (EventSource, same cookie)
    N->>API: proxy, buffering off
    API->>API: authenticate, register a queue for user_id
    API-->>B: 200 text/event-stream (stays open)
    W->>DB: COMMIT including pg_notify with check_id, owner_id, state, rt_ms
    DB-->>L: notification
    L->>API: put event into queues of user owner_id and public pages listing check_id
    API-->>B: event check, data with the JSON payload
    B->>B: patch TanStack Query cache, UI re-renders
    loop every 15 s
        API-->>B: comment ping, keeps proxies from closing an idle stream
    end
    Note over B,API: Connection drops → EventSource reconnects automatically → browser refetches the snapshot. Missed events are never replayed, the snapshot covers them.
```

**Notes:**
- The payload carries `owner_id`, so the listener can route events without a
  database query.
- A public page's stream is filtered by the set of `check_id`s that page lists,
  loaded when the stream connects. Changes to the page config apply on reconnect.
- "How long has it been down" is computed in the browser from the incident's
  `started_at`. The server doesn't tick it.

---

## (d) Worker crash: the lease expires and another worker takes over

```mermaid
sequenceDiagram
    autonumber
    participant W1 as Worker 1
    participant DB as PostgreSQL
    participant W2 as Worker 2
    participant S as Target site

    W1->>DB: claim check 17 → lease_owner = w1, lease_until = now + timeout + 5 s
    W1->>S: GET (in flight)
    Note over W1: process dies (OOM, kill -9, host failure)
    loop every 1 s while the lease is valid
        W2->>DB: claim query
        DB-->>W2: check 17 skipped, lease still valid
    end
    W2->>DB: claim query after lease_until
    DB-->>W2: check 17 → lease_owner = w2
    W2->>S: GET
    W2->>DB: completion tx, lease_owner = w2 matches, result stored
    Note over DB: The run lost with W1 left no result row: it shows as a gap, never as a failure
    rect rgba(128, 128, 128, 0.12)
        Note over W1,W2: Zombie variant: W1 was only stalled, not dead
        W1->>DB: completion tx: SELECT ... WHERE lease_owner = w1 FOR UPDATE
        DB-->>W1: 0 rows (fenced out)
        W1->>DB: ROLLBACK, stale result discarded
    end
```

A **zombie** can't actually outlive its lease, because the hard
`asyncio.timeout` (= the check's timeout) is shorter than the lease
(timeout + 5 s). The fencing check is the second line of defence, for cases
like the event loop being blocked.

---

## (e) Login and authenticated requests

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant API as API
    participant DB as PostgreSQL

    B->>API: POST /api/auth/login (JSON email + password)
    API->>DB: SELECT user WHERE lower(email) = lower(input)
    DB-->>API: id, password_hash
    API->>API: argon2 verify, sign JWT with sub = user_id and exp
    API-->>B: 204, Set-Cookie access_token, HttpOnly, Secure, SameSite=Lax, Path=/
    B->>API: GET /api/checks/42 (cookie sent automatically)
    API->>API: current_user dependency verifies JWT → user_id
    API->>DB: SELECT ... FROM checks WHERE id = 42 AND owner_id = user_id (scoped repo)
    alt not found, or someone else's
        API-->>B: 404 (never 403: don't reveal that it exists)
    else found
        API-->>B: 200 check
    end
    B->>API: GET /api/events (EventSource sends the same cookie)
    B->>API: POST /api/auth/logout
    API-->>B: 204, Set-Cookie access_token empty, Max-Age=0
```

**Notes:**
- **CSRF:** `SameSite=Lax` stops the cookie being sent on cross-site POSTs. The
  API also accepts only JSON request bodies on changing requests, and a
  cross-site HTML form can't send JSON.
- **`Secure` over plain http on localhost** works, because browsers treat
  localhost as a secure context. Make it configurable with `COOKIE_SECURE`.
- **Trade-off:** a stateless JWT can't be revoked before it expires. Logout only
  clears the cookie. Keep the expiry short-ish (for example 24 h) and document it.

---

## (f) Pause while a run is in flight: the lock-order rule

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser
    participant API as API
    participant DB as PostgreSQL
    participant T as Worker: check task

    Note over T: check 17 is DOWN and a run is in flight
    U->>API: POST /api/checks/17/pause
    API->>DB: BEGIN
    API->>DB: SELECT check_state WHERE check_id = 17 FOR UPDATE (lock first)
    API->>DB: UPDATE checks SET is_paused = true
    API->>DB: UPDATE check_state SET state = 'pending', counters reset
    API->>DB: UPDATE incidents SET ended_at = now(), end_reason = 'paused'
    API->>DB: pg_notify, COMMIT
    API-->>U: 204
    T->>DB: completion tx: SELECT check_state + checks FOR UPDATE (waits while the API holds the lock)
    DB-->>T: is_paused = true
    T->>DB: clear lease only, discard result, COMMIT
    Note over API,T: If the worker locks first, the API waits instead. Its UPDATE then sees the worker's committed changes, for example an incident the worker just opened, and closes that incident.
```

**Why it's correct either way round:** both sides lock the same row first, so
one transaction always runs completely before the other. Whichever goes second
works from the other's committed result.
