# Sakshy

Website uptime monitor: add sites to check, see their status live, get an email when one goes
down and when it recovers.

## Run it locally

You need Docker with Compose v2. Ports 8080, 8000, 8025, 9000, 5432 and 1025 must be free.

```sh
git clone git@github.com:bidahmetov02/Sakshy.git sakshy
cd sakshy
docker compose up -d --build
```

The first build takes a few minutes. It's ready when `docker compose ps` shows `api` as
`healthy`.

| What | Where |
|---|---|
| App | http://localhost:8080 |
| Emails (Mailpit) | http://localhost:8025 |
| API docs | http://localhost:8000/api/docs |

Sign up at http://localhost:8080. Or load demo data (a user, checks in every state, 30 days of
history and a public status page):

```sh
docker compose run --rm seed
```

Then log in as `demo@example.com` / `demo-password`. The public page is at
http://localhost:8080/status/demo.

## Watch a site go down

The `emulator` service plays the monitored sites. Add a check with the URL
`http://emulator:9000/sites/shop`, then break and fix that site:

```sh
curl -X PUT localhost:9000/control/sites/shop -H 'content-type: application/json' -d '{"mode":"error"}'
curl -X PUT localhost:9000/control/sites/shop -H 'content-type: application/json' -d '{"mode":"ok"}'
```

Modes: `ok`, `error`, `slow`, `hang`. A site counts as down after 3 failed checks in a row
(about 20 s; **Run now** skips the wait for the first one). The DOWN and UP emails show up in
Mailpit.

Checks can't target private addresses (localhost, 10.x, 192.168.x and so on). Only the hosts in
`PROBE_ALLOWED_HOSTS` in `docker-compose.yml` are exempt (`emulator` by default).

## Load test

Checks on a 30 s interval against the emulator: 80% fast, 15% slow (1.5 s), 5% hanging (5 s
timeout). Docker on a laptop with 8 CPUs and 8 GB. "Late" is how long the most overdue check
waits to be picked up. Up to about 1 s is on time, because idle workers look for due checks
once a second.

| Checks | Workers | Runs/s (target / done) | Late p95 | API p95 | Postgres CPU |
|---|---|---|---|---|---|
| 3,000 | 1 | 100 / 100 | 0.9 s | 21 ms | 40% |
| 6,000 | 1 | 200 / 183 | 17.7 s, growing | 10 ms | 112% |
| 6,000 | 2 | 200 / 198 | 0.9 s | 22 ms | 56% |
| 9,000 | 2 | 300 / 300 | 1.0 s | 18 ms | 160% |

- **One worker handles about 5,000 checks.** The limit is its 100 concurrent checks
  (`WORKER_CONCURRENCY`). With the average check taking about 0.5 s, that's about 200 runs/s.
  CPU and the database are not the limit. Add workers (`docker compose up -d --scale worker=2`)
  or raise the concurrency.
- **The dashboard API stays fast** (p95 under 25 ms) at every load.
- **The test found a bug, now fixed.** A worker used to wait 1 s even after taking a full
  batch, which capped it at about 70 runs/s (3,000 checks fell 37 s behind). It now picks up
  more checks as soon as slots free up.

Run it with the stack up (needs [uv](https://docs.astral.sh/uv/)):
`cd backend && uv run python -m tools.loadtest 3000 6000 9000`. It deletes its data afterwards.

## Stop

```sh
docker compose down      # keep the data
docker compose down -v   # also delete the database
```
