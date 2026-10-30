# Architecture

## Container diagram

Every box is one service in `docker-compose.yml`. Each arrow is labelled with
what travels over it.

```mermaid
flowchart TB
    owner(["Owner<br/>browser"])
    visitor(["Public visitor<br/>browser"])

    subgraph compose["docker compose"]
        fe["<b>frontend</b><br/>nginx: serves React SPA,<br/>proxies /api/*"]
        api["<b>api</b><br/>FastAPI + uvicorn<br/>REST · SSE · auth"]
        migrate["<b>migrate</b><br/>alembic upgrade head<br/>runs once, then exits"]
        db[("<b>db</b><br/>PostgreSQL 18")]
        worker["<b>worker</b><br/>asyncio process<br/>scheduler · checker<br/>alerts · housekeeping"]
        mail["<b>mailpit</b><br/>SMTP sink + web UI :8025"]
        emu["<b>emulator</b><br/>fake target sites"]
    end

    sites(["Real websites<br/>internet"])

    owner -- "HTTPS: SPA, REST, SSE" --> fe
    visitor -- "HTTPS: public page + SSE" --> fe
    fe -- "proxy /api/*<br/>buffering off for SSE" --> api
    api <-- "SQL pool<br/>+ 1 LISTEN connection" --> db
    migrate -- "DDL" --> db
    worker <-- "SQL: claim, complete,<br/>pg_notify" --> db
    worker -- "SMTP" --> mail
    worker -- "HTTP probes" --> emu
    worker -- "HTTP(S) probes" --> sites
```

Reviewers read the alert emails in Mailpit's web UI (:8025) and flip the
emulator's targets between healthy, slow, erroring and hanging through its
control endpoint, so they can watch a site go down and recover live.

**Startup order:** `db` (healthcheck) → `migrate` (must complete successfully)
→ `api` and `worker` → `frontend`.

**Key properties:**
- **The API never runs checks; the worker never serves HTTP.** A slow site or a
  burst of checks can't slow down the UI.
- **The database is the only shared state.** API and worker processes are
  stateless, so each can be scaled: `docker compose up --scale worker=3`.
- **Same origin for the browser:** nginx serves the SPA and proxies `/api`, so
  there is no CORS and the auth cookie just works.
- **Realtime path:** the worker commits with `pg_notify` → Postgres delivers to
  the API's LISTEN connection → the API pushes to browsers over SSE.

| Service | Port (host) | Notes |
|---|---|---|
| frontend | 8080 | the only port reviewers need |
| api | (internal) 8000 | expose it in dev for `/docs` |
| db | (internal) 5432 | expose it in dev for psql |
| mailpit | 8025 (UI), internal 1025 (SMTP) | |
| emulator | (internal) 9000 | expose it for the control endpoint |

## Worker internals

One process, one asyncio event loop. Each box is a long-running asyncio task.
They coordinate **only through the database**, never through in-memory state,
which is what makes running N workers safe.

```mermaid
flowchart TB
    subgraph proc["worker process: one asyncio event loop"]
        direction TB
        startup["<b>Startup</b><br/>enlarge DNS thread pool<br/>jitter checks overdue after downtime<br/>register heartbeat"]
        claim["<b>Claim loop</b> · every 1 s<br/>UPDATE check_state ... SKIP LOCKED<br/>sets lease_owner, lease_until"]
        sem{{"Semaphore<br/>max ~100 in flight"}}
        subgraph task["Check task: one per claimed check"]
            direction TB
            probe["<b>HTTP probe</b><br/>shared httpx client, no keep-alive<br/>hard asyncio.timeout"]
            evaluate["<b>Evaluate</b><br/>status code, body substring<br/>ok or error_kind"]
            sm["<b>State machine</b><br/>pure function<br/>state + result → new state + events"]
            tx["<b>Completion transaction</b><br/>lock check_state, verify lease<br/>result, rollup, state<br/>incident, outbox, pg_notify"]
            probe --> evaluate --> sm --> tx
        end
        sender["<b>Outbox sender</b> · every 2 s<br/>claim due emails, send via SMTP<br/>retry with backoff"]
        sweep["<b>Maintenance sweep</b> · every 15 s<br/>open + unalerted + no active window<br/>enqueue DOWN email"]
        house["<b>Housekeeping</b> · hourly<br/>create partitions ahead, drop old ones<br/>purge soft-deleted checks"]
        hb["<b>Heartbeat</b> · every 10 s"]
        startup --> claim
        claim --> sem --> probe
    end
    db[("PostgreSQL")]
    smtp["SMTP / Mailpit"]
    targets["Target sites"]

    claim <--> db
    tx --> db
    sender <--> db
    sender --> smtp
    sweep <--> db
    house --> db
    hb --> db
    probe --> targets
```

| Task | Runs | Reads | Writes | Why it's separate |
|---|---|---|---|---|
| Claim loop | every 1 s | due, unleased, active checks | lease on `check_state` | one cheap query per tick, whatever the number of checks |
| Check task | per claim | target site | everything in the completion tx | a slow site only blocks its own task |
| Outbox sender | every 2 s | `notifications` due | status, attempts | a slow SMTP server never delays checks |
| Maintenance sweep | every 15 s | open unalerted incidents, windows | outbox rows, `down_alert_queued` | window end is a time event, not a check result |
| Housekeeping | hourly | partition list, soft-deleted checks | partitions, hard deletes | heavy and rare |
| Heartbeat | every 10 s | none | `worker_heartbeats` | lets the UI mark "monitoring was offline" |

**Shared inside the process:** one `httpx.AsyncClient`, one SQLAlchemy async
engine (asyncpg driver), the semaphore, and the enlarged default thread pool
used for DNS lookups.

**Database access:** the API uses the SQLAlchemy ORM for CRUD. The worker's hot
paths (claim, completion transaction, sweep) use SQLAlchemy Core with explicit
SQL. The API's LISTEN connection is a raw asyncpg connection.

**Graceful shutdown (SIGTERM):** stop claiming, wait up to about 10 s for
in-flight checks, then exit. Leases on anything unfinished simply expire.

**Lock-order rule:** every transaction that changes a check's runtime state
(completion, pause, delete, sweep) locks that check's `check_state` row
**first**. With a single order everywhere there are no deadlocks, and every
decision is made on the latest committed state. The sweep uses `SKIP LOCKED`,
so it never waits: it just retries the check next round.
