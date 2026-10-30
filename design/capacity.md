# Capacity sheet

Back-of-envelope numbers. They tell us **which part breaks first** as the
number of checks grows, so we design for it now and measure it later.

## Assumptions

| Symbol | Value | Note |
|---|---|---|
| Interval | 30 s for every check | worst case allowed by the requirements |
| Runs per check per day | 2,880 | 86,400 s / 30 s |
| Row writes per run | 4 | claim UPDATE, result INSERT, rollup UPSERT, state UPDATE |
| Raw row size | ~110 bytes | ~35 B data + ~24 B row header + ~40 B primary-key index entry |
| Rollup row size | ~100 bytes | one row per check per hour |
| Raw retention | 7 days | partitions dropped after that |
| Typical response time | 300 ms | |
| Timeout | 10 s | used for the worst case |

Retries add 2 extra runs per incident. That's negligible unless many sites fail at once.

## Formulas

```
runs/s             = checks / 30
row writes/s       = runs/s × 4
transactions/s     ≈ runs/s            (one completion tx per run; claims are batched)
raw rows/day       = checks × 2,880
rollup rows/month  = checks × 720
in-flight HTTP     = runs/s × response time      (Little's law)
```

## Numbers

| | **50** (the requirement) | **1,000** | **10,000** | **100,000** |
|---|---|---|---|---|
| Check runs / s | 1.7 | 33 | 333 | 3,333 |
| Row writes / s | 7 | 133 | 1,333 | 13,333 |
| Completion transactions / s | 1.7 | 33 | 333 | 3,333 |
| NOTIFY / s (one per run, unbatched) | 1.7 | 33 | 333 | 3,333 |
| Raw rows / day | 144 k | 2.9 M | 29 M | 288 M |
| Raw storage / day | 16 MB | 0.3 GB | 3.2 GB | 32 GB |
| Raw storage, 7-day retention | 0.1 GB | 2.2 GB | 22 GB | 222 GB |
| Rollup rows / month | 36 k | 720 k | 7.2 M | 72 M |
| Rollup storage / month | 4 MB | 72 MB | 0.7 GB | 7 GB |
| In-flight HTTP, typical (300 ms) | 0.5 | 10 | 100 | 1,000 |
| In-flight HTTP, worst (all time out at 10 s) | 17 | 333 | 3,333 | 33,333 |
| Workers needed (estimate, to be measured) | 1 | 1 | 2–3, batched writes | 10+, batched writes, TimescaleDB |

## What breaks first, in order

1. **~1k–10k checks: transactions per second and WAL volume.** One commit per
   run, each writing 4 rows plus index entries. Fix: batch completions, for
   example commit every 100 ms or every 50 results, pre-aggregating the rollup
   per `(check_id, bucket)` in Python first.
2. **~10k: `check_state` bloat.** Every check's row is updated twice a minute.
   `next_run_at` is indexed, so these can't be HOT (in-place) updates and each
   one leaves a dead row version. Fix: tune autovacuum for that table only.
3. **~10k, during big outages: in-flight HTTP.** When many sites time out at
   once, in-flight requests jump 30× (the "worst" row). The semaphore caps
   them; past that, add workers. Watch the DNS thread pool here.
4. **~10k–100k: NOTIFY.** A transaction that issues NOTIFY takes a global lock
   at commit, which serialises those commits. Fix: one NOTIFY per batch; past
   that, move the event bus to Redis or NATS.
5. **~100k: TLS handshake CPU** (cold connections) and **raw storage**. Fix:
   more workers; TimescaleDB with compression.
6. **Always: a single Postgres.** It's the single point of failure. Out of scope for this assessment.

## Things that do not grow with history

- Dashboard: reads `check_state` only. That's O(checks), whatever history exists.
- Week and month charts: O(buckets) from rollups: 168 or 720 rows per check.
- Uptime %: a sum over rollups.

## How to verify (later)

A load test using the emulator: seed N checks pointing at `/ok`, `/slow` and
`/hang` in fixed ratios, then measure check-run lag (`now() - next_run_at` at
claim time), commits/s, and API p95 latency while it runs.
