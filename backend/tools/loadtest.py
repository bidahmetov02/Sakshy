"""Load test: N checks against the emulator, run by the compose worker(s), measured from outside.

With the stack up, from backend/:

    uv run python -m tools.loadtest 1000 2000 3000

Each step tops the load checks up to N (30 s interval, 80% /ok, 15% /slow 1.5 s, 5% /hang with
a 5 s timeout), waits a warm-up, then measures. Prints one markdown table row per step.
Load users, checks, results and their alert emails are deleted at the end (and at the start,
after an interrupted run). Scale workers with `docker compose up -d --scale worker=2`.
"""

import argparse
import asyncio
import statistics
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from api.security import COOKIE, hash_password
from core.db import create_engine
from core.settings import get_settings

DOMAIN = "loadtest.example.com"
PASSWORD = "loadtest-password"  # noqa: S105 (throwaway local users)
ALERT_TO = f"alerts@{DOMAIN}"
CHECKS_PER_USER = 50
INTERVAL_S = 30
TIMEOUT_S = 5
EMULATOR = "http://emulator:9000"
API = "http://localhost:8080/api"
MAILPIT = "http://localhost:8025"

LOAD_CHECKS = f"""
    SELECT c.id FROM checks c JOIN users u ON u.id = c.owner_id
    WHERE u.email LIKE '%@{DOMAIN}'
"""


@dataclass
class Step:
    checks: int
    workers: int
    runs_per_s: float
    late_p95_s: float
    late_max_s: float
    commits_per_s: float
    api_p50_ms: float
    api_p95_ms: float
    cpu: str

    def row(self) -> str:
        target = self.checks / INTERVAL_S
        return (
            f"| {self.checks:,} | {self.workers} | {target:.0f} | {self.runs_per_s:.0f} "
            f"| {self.late_p95_s:.1f} s | {self.late_max_s:.1f} s | {self.commits_per_s:.0f} "
            f"| {self.api_p50_ms:.0f} / {self.api_p95_ms:.0f} ms | {self.cpu} |"
        )


HEADER = (
    "| Checks | Workers | Target runs/s | Actual runs/s | Late p95 | Late max | Commits/s "
    "| API p50 / p95 | CPU |\n|---|---|---|---|---|---|---|---|---|"
)


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        # Raw results have no foreign key (partitioned): delete them explicitly.
        await conn.execute(text(f"DELETE FROM check_results WHERE check_id IN ({LOAD_CHECKS})"))
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%@{DOMAIN}'"))
    async with httpx2.AsyncClient(base_url=MAILPIT) as mailpit:
        await mailpit.delete("/api/v1/search", params={"query": f'to:"{ALERT_TO}"'})


async def top_up(engine: AsyncEngine, checks: int, password_hash: str) -> None:
    """Add load checks up to `checks`, due at random times over the next interval."""
    have = int(await scalar(engine, f"SELECT count(*) FROM ({LOAD_CHECKS}) x"))
    if have >= checks:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(f"""
                INSERT INTO users (email, password_hash)
                SELECT 'load-' || u || '@{DOMAIN}', :hash
                FROM generate_series(0, (:n - 1) / {CHECKS_PER_USER}) u
                ON CONFLICT DO NOTHING
            """),
            {"hash": password_hash, "n": checks},
        )
        await conn.execute(
            text(f"""
                WITH new AS (
                    INSERT INTO checks (owner_id, name, url, interval_s, timeout_s, alert_emails)
                    SELECT u.id, 'load-' || g,
                        CASE WHEN g % 100 < 80 THEN '{EMULATOR}/ok'
                             WHEN g % 100 < 95 THEN '{EMULATOR}/slow?ms=1500'
                             ELSE '{EMULATOR}/hang' END,
                        {INTERVAL_S}, {TIMEOUT_S}, ARRAY['{ALERT_TO}']
                    FROM generate_series(:have, :n - 1) g
                    JOIN users u ON u.email = 'load-' || (g / {CHECKS_PER_USER}) || '@{DOMAIN}'
                    RETURNING id
                )
                INSERT INTO check_state (check_id, next_run_at)
                SELECT id, now() + random() * interval '{INTERVAL_S} s' FROM new
            """),
            {"have": have, "n": checks},
        )


async def sample_lateness(engine: AsyncEngine, stop: asyncio.Event, out: list[float]) -> None:
    """Every second: how long the most overdue unclaimed load check has been waiting."""
    sql = f"""
        SELECT coalesce(extract(epoch FROM max(now() - s.next_run_at)), 0)
        FROM check_state s
        WHERE s.check_id IN ({LOAD_CHECKS}) AND s.next_run_at < now()
          AND (s.lease_until IS NULL OR s.lease_until < now())
    """
    while not stop.is_set():
        out.append(float(await scalar(engine, sql)))
        await asyncio.sleep(1)


async def sample_api(stop: asyncio.Event, out: list[float]) -> None:
    """The dashboard request of one load user (50 checks), four times a second."""
    async with httpx2.AsyncClient(base_url=API, timeout=10) as client:
        login = {"email": f"load-0@{DOMAIN}", "password": PASSWORD}
        response = (await client.post("/auth/login", json=login)).raise_for_status()
        # The cookie is Secure: browsers send it to http://localhost, httpx doesn't.
        client.headers["Cookie"] = f"{COOKIE}={response.cookies[COOKIE]}"
        while not stop.is_set():
            started = time.perf_counter()
            (await client.get("/checks")).raise_for_status()
            out.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(0.25)


def docker_cpu() -> str:
    """CPU per container right now, e.g. "worker 85%, db 30%"."""
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    parts: list[str] = []
    for line in stats:
        if not line.startswith("sakshy-"):
            continue
        name, cpu = line.split()
        service = name.removeprefix("sakshy-").rsplit("-", 1)[0]
        if service in {"worker", "db", "api", "emulator"}:
            parts.append(f"{service} {float(cpu.rstrip('%')):.0f}%")
    return ", ".join(sorted(parts))


def worker_count() -> int:
    ids = subprocess.run(
        ["docker", "compose", "ps", "-q", "worker"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return len(ids)


async def measure(engine: AsyncEngine, checks: int, seconds: float) -> Step:
    commits_sql = "SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()"
    runs_sql = (
        f"SELECT count(*) FROM check_results "
        f"WHERE check_id IN ({LOAD_CHECKS}) AND checked_at >= :t0"
    )
    t0 = await scalar(engine, "SELECT now()")
    commits0 = int(await scalar(engine, commits_sql))
    started = time.monotonic()

    stop = asyncio.Event()
    lateness: list[float] = []
    api: list[float] = []
    samplers = [
        asyncio.create_task(sample_lateness(engine, stop, lateness)),
        asyncio.create_task(sample_api(stop, api)),
    ]
    await asyncio.sleep(seconds / 2)
    cpu = await asyncio.to_thread(docker_cpu)
    await asyncio.sleep(seconds / 2)
    stop.set()
    await asyncio.gather(*samplers)

    elapsed = time.monotonic() - started
    runs = int(await scalar(engine, runs_sql, t0=t0))
    commits = int(await scalar(engine, commits_sql)) - commits0
    late = sorted(lateness)
    return Step(
        checks=checks,
        workers=worker_count(),
        runs_per_s=runs / elapsed,
        late_p95_s=late[int(len(late) * 0.95)],
        late_max_s=late[-1],
        commits_per_s=commits / elapsed,
        api_p50_ms=statistics.median(api),
        api_p95_ms=statistics.quantiles(api, n=20)[-1],
        cpu=cpu,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load test against the compose stack.")
    parser.add_argument("checks", type=int, nargs="+", help="check counts, one step each")
    parser.add_argument("--warmup", type=float, default=45, help="seconds before measuring")
    parser.add_argument("--measure", type=float, default=120, help="seconds measured per step")
    parser.add_argument("--keep", action="store_true", help="keep the load data afterwards")
    args = parser.parse_args()

    engine = create_engine(str(get_settings().database_url))
    try:
        await cleanup(engine)
        password_hash = await hash_password(PASSWORD)
        print(HEADER, flush=True)
        for checks in sorted(args.checks):
            await top_up(engine, checks, password_hash)
            await asyncio.sleep(args.warmup)
            print((await measure(engine, checks, args.measure)).row(), flush=True)
    finally:
        if not args.keep:
            await cleanup(engine)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
