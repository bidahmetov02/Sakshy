"""History: chart buckets, raw results and the incident log."""

from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import IncidentEndReason
from tests.conftest import create_check, user_id
from tests.factories import add_group, add_incident, add_result, add_rollup

# The 5-minute slot that started an hour before the current one.
SLOT = "date_bin('5 min', now(), timestamptz '2000-01-01 00:00+00') - interval '1 hour'"


async def sql_time(engine: AsyncEngine, expression: str) -> datetime:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT {expression}"))).scalar_one()


@pytest.fixture
async def check(alice: httpx2.AsyncClient) -> dict[str, Any]:
    return await create_check(alice)


# ---------- Stats ----------


async def test_day_stats_from_raw_results_without_retries(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    check_id = check["id"]
    await add_result(engine, check_id, f"{SLOT} + interval '10 s'", ok=True, response_ms=100)
    await add_result(engine, check_id, f"{SLOT} + interval '20 s'", ok=False, response_ms=201)
    await add_result(engine, check_id, f"{SLOT} + interval '30 s'", ok=False, is_retry=True)
    await add_result(engine, check_id, f"{SLOT} + interval '40 s'", ok=False, response_ms=None)
    await add_result(engine, check_id, "now() - interval '25 hours'", ok=False)  # too old

    stats = (await alice.get(f"/api/checks/{check_id}/stats", params={"range": "day"})).json()

    buckets = stats["buckets"]
    assert len(buckets) == 288
    slot = await sql_time(engine, SLOT)
    [filled] = [b for b in buckets if b["total"] is not None]
    assert datetime.fromisoformat(filled["t"]) == slot
    # avg and max over the runs that had a response time
    assert filled | {"t": None} == {"t": None, "total": 3, "ok": 1, "avg_ms": 151, "max_ms": 201}
    assert stats["uptime_pct"] == pytest.approx(100 / 3)
    gap = buckets[0]
    assert gap | {"t": None} == {
        "t": None,
        "total": None,
        "ok": None,
        "avg_ms": None,
        "max_ms": None,
    }
    last = datetime.fromisoformat(buckets[-1]["t"])
    assert last == slot + timedelta(hours=1)  # the current slot comes last
    assert last - datetime.fromisoformat(buckets[0]["t"]) == timedelta(minutes=5 * 287)


@pytest.mark.parametrize(("range_", "slots"), [("week", 168), ("month", 720)])
async def test_week_and_month_stats_from_rollups(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any], range_: str, slots: int
) -> None:
    check_id = check["id"]
    await add_rollup(engine, check_id, hours_ago=0, total=120, ok=119, rt_ms=[100, 201])
    await add_rollup(engine, check_id, hours_ago=200, total=120, ok=0, rt_ms=[])  # month only

    stats = (await alice.get(f"/api/checks/{check_id}/stats", params={"range": range_})).json()

    buckets = stats["buckets"]
    assert len(buckets) == slots
    assert buckets[-1] | {"t": None} == {
        "t": None,
        "total": 120,
        "ok": 119,
        "avg_ms": 151,  # 150.5, rounded
        "max_ms": 201,
    }
    filled = [b for b in buckets if b["total"] is not None]
    if range_ == "week":
        assert len(filled) == 1
        assert stats["uptime_pct"] == pytest.approx(100 * 119 / 120)
    else:
        assert [(b["total"], b["ok"], b["avg_ms"]) for b in filled[:1]] == [(120, 0, None)]
        assert stats["uptime_pct"] == pytest.approx(100 * 119 / 240)


async def test_stats_without_runs(alice: httpx2.AsyncClient, check: dict[str, Any]) -> None:
    stats = (await alice.get(f"/api/checks/{check['id']}/stats")).json()  # day by default

    assert stats["uptime_pct"] is None
    assert all(b["total"] is None for b in stats["buckets"])


async def test_stats_validates_and_scopes(
    alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    path = f"/api/checks/{check['id']}/stats"
    assert (await alice.get(path, params={"range": "year"})).status_code == 422
    assert (await bob.get(path)).status_code == 404


# ---------- Results ----------


async def test_results_newest_first_in_pages(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    for minutes in range(5):
        await add_result(
            engine,
            check["id"],
            f"now() - interval '{minutes} min'",
            ok=minutes != 1,
            is_retry=minutes == 1,
        )
    path = f"/api/checks/{check['id']}/results"

    pages: list[list[dict[str, Any]]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await alice.get(path, params=params)).json()
        pages.append(page["items"])
        if (cursor := page["next_cursor"]) is None:
            break

    assert [len(p) for p in pages] == [2, 2, 1]
    results = [r for p in pages for r in p]
    times = [r["checked_at"] for r in results]
    assert times == sorted(times, reverse=True)
    assert [r["is_retry"] for r in results] == [False, True, False, False, False]
    assert set(results[0]) == {
        "checked_at",
        "ok",
        "response_ms",
        "status_code",
        "error_kind",
        "error_message",
        "is_retry",
    }


async def test_results_validate_and_scope(
    alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    path = f"/api/checks/{check['id']}/results"
    assert (await alice.get(path, params={"cursor": "garbage"})).status_code == 422
    for limit in (0, 201):
        assert (await alice.get(path, params={"limit": limit})).status_code == 422
    assert (await bob.get(path)).status_code == 404
    assert (await alice.get(path)).json() == {"items": [], "next_cursor": None}


# ---------- Incidents ----------


@pytest.fixture
async def incidents(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> dict[str, int]:
    group_id = await add_group(engine, await user_id(alice))
    grouped = (await create_check(alice, group_id=group_id))["id"]
    other = (await create_check(alice))["id"]
    deleted = (await create_check(alice))["id"]
    closed = {"end_reason": IncidentEndReason.RECOVERED}
    ids = {
        "group_id": group_id,
        "old": await add_incident(
            engine,
            grouped,
            started_at=text("now() - interval '2 days'"),
            detected_at=text("now() - interval '2 days'"),
            ended_at=text("now() - interval '2 days' + interval '90 s'"),
            **closed,
        ),
        "recent": await add_incident(
            engine,
            grouped,
            started_at=text("now() - interval '3 hours'"),
            detected_at=text("now() - interval '3 hours'"),
            ended_at=text("now() - interval '2 hours'"),
            down_alert_queued=False,
            began_in_maintenance=True,
            **closed,
        ),
        "open": await add_incident(engine, other),  # started 10 min ago
    }
    await add_incident(engine, deleted)
    await alice.delete(f"/api/checks/{deleted}")
    await add_incident(engine, (await create_check(bob))["id"])
    return ids


async def ids(client: httpx2.AsyncClient, **params: Any) -> list[int]:
    response = await client.get("/api/incidents", params=params)
    assert response.status_code == 200, response.text
    return [i["id"] for i in response.json()["items"]]


async def test_incident_log(alice: httpx2.AsyncClient, incidents: dict[str, int]) -> None:
    log = (await alice.get("/api/incidents")).json()

    assert [i["id"] for i in log["items"]] == [incidents[k] for k in ("open", "recent", "old")]
    open_, recent, old = log["items"]
    assert (open_["ended_at"], open_["duration_s"], open_["end_reason"]) == (None, None, None)
    assert (open_["cause_kind"], open_["cause_message"]) == ("timeout", "no response within 10 s")
    assert (recent["duration_s"], recent["end_reason"]) == (3600, "recovered")
    assert (recent["alerted"], recent["began_in_maintenance"]) == (False, True)
    assert (old["duration_s"], old["alerted"]) == (90, True)


async def test_incident_filters(alice: httpx2.AsyncClient, incidents: dict[str, int]) -> None:
    assert await ids(alice, group_id=incidents["group_id"]) == [
        incidents["recent"],
        incidents["old"],
    ]
    log = (await alice.get("/api/incidents")).json()["items"]
    open_check = log[0]["check_id"]
    assert await ids(alice, check_id=open_check) == [incidents["open"]]
    assert await ids(alice, check_id=999_999) == []


async def test_incidents_overlapping_a_period(
    engine: AsyncEngine, alice: httpx2.AsyncClient, incidents: dict[str, int]
) -> None:
    def iso(at: datetime) -> str:
        return at.isoformat()

    now = await sql_time(engine, "now()")
    # ended 2 h ago: out; the open one overlaps any period up to now
    assert await ids(alice, **{"from": iso(now - timedelta(minutes=90))}) == [incidents["open"]]
    # started before 2.5 h ago: the recent one (still going then) and the old one
    assert await ids(alice, to=iso(now - timedelta(minutes=150))) == [
        incidents["recent"],
        incidents["old"],
    ]
    period = {"from": iso(now - timedelta(days=1)), "to": iso(now - timedelta(hours=1))}
    assert await ids(alice, **period) == [incidents["recent"]]
    naive = await alice.get("/api/incidents", params={"from": "2026-11-01T10:00:00"})
    assert naive.status_code == 422


async def test_incident_pages(alice: httpx2.AsyncClient, incidents: dict[str, int]) -> None:
    first = (await alice.get("/api/incidents", params={"limit": 2})).json()
    rest = (
        await alice.get("/api/incidents", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()

    assert [i["id"] for i in first["items"]] == [incidents["open"], incidents["recent"]]
    assert ([i["id"] for i in rest["items"]], rest["next_cursor"]) == ([incidents["old"]], None)
    assert (await alice.get("/api/incidents", params={"cursor": "e30"})).status_code == 422
