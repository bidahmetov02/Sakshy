"""Maintenance windows: targets, the database clock ("a past time means now"), editing
rules, and the overlay on checks."""

from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import create_check, user_id
from tests.factories import add_group, add_incident, add_window
from worker.sweep import sweep

HOUR = timedelta(hours=1)


async def now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.now()))).scalar_one()


def iso(at: datetime) -> str:
    return at.isoformat()


async def create_window(client: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/maintenance", json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def check(alice: httpx2.AsyncClient) -> dict[str, Any]:
    return await create_check(alice)


async def test_create_on_a_check(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    t = await now(engine)
    window = await create_window(
        alice,
        check_id=check["id"],
        starts_at=iso(t + HOUR),
        ends_at=iso(t + 2 * HOUR),
        reason=" deploy ",
    )

    times = {"starts_at", "ends_at"}
    assert {k: v for k, v in window.items() if k not in times | {"id", "created_at"}} == {
        "check_id": check["id"],
        "group_id": None,
        "reason": "deploy",
        "status": "upcoming",
    }
    assert [datetime.fromisoformat(window[k]) for k in ("starts_at", "ends_at")] == [
        t + HOUR,
        t + 2 * HOUR,
    ]


async def test_a_past_start_means_now(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    t = await now(engine)
    window = await create_window(
        alice, check_id=check["id"], starts_at=iso(t - HOUR), ends_at=iso(t + HOUR)
    )

    assert window["status"] == "active"
    started = datetime.fromisoformat(window["starts_at"])
    assert t <= started < t + timedelta(seconds=5)


async def test_create_on_a_group_puts_its_checks_in_maintenance(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    group_id = await add_group(engine, await user_id(alice))
    member = await create_check(alice, group_id=group_id)
    other = await create_check(alice)
    t = await now(engine)

    await create_window(alice, group_id=group_id, starts_at=iso(t), ends_at=iso(t + HOUR))

    checks = {c["id"]: c for c in (await alice.get("/api/checks")).json()}
    assert checks[member["id"]]["in_maintenance"] is True
    assert checks[other["id"]]["in_maintenance"] is False


@pytest.mark.parametrize(
    "body",
    [
        {},  # no target
        {"group_id": 1},  # and check_id: two targets
        {"ends_at": "past"},
        {"ends_at": "before_start"},
        {"starts_at": "2026-11-01T10:00:00"},  # no time zone
        {"reason": "x" * 501},
    ],
)
async def test_create_validates(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any], body: dict[str, Any]
) -> None:
    t = await now(engine)
    times = {"past": iso(t - timedelta(seconds=1)), "before_start": iso(t + HOUR / 2)}
    window = {"check_id": check["id"], "starts_at": iso(t + HOUR), "ends_at": iso(t + 2 * HOUR)}
    window |= {k: times.get(v, v) if isinstance(v, str) else v for k, v in body.items()}
    if not body:
        del window["check_id"]
    if "group_id" in body:
        window["group_id"] = await add_group(engine, await user_id(alice))

    assert (await alice.post("/api/maintenance", json=window)).status_code == 422


async def test_targets_must_be_the_users_live_check_or_group(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    bobs_check = await create_check(bob)
    bobs_group = await add_group(engine, await user_id(bob))
    deleted = await create_check(alice)
    await alice.delete(f"/api/checks/{deleted['id']}")
    t = await now(engine)
    times = {"starts_at": iso(t), "ends_at": iso(t + HOUR)}

    for field, target in [
        ("check_id", bobs_check["id"]),
        ("check_id", deleted["id"]),
        ("check_id", 999_999),
        ("group_id", bobs_group),
    ]:
        response = await alice.post("/api/maintenance", json=times | {field: target})
        assert response.status_code == 422, (field, target)
        assert response.json()["detail"][0]["loc"] == ["body", field]


async def test_list_filters_by_status(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    owner = await user_id(alice)
    check_id = (await create_check(alice))["id"]
    past = await add_window(
        engine,
        owner,
        check_id=check_id,
        starts_at=text("now() - interval '3 hours'"),
        ends_at=text("now() - interval '2 hours'"),
    )
    active = await add_window(engine, owner, check_id=check_id)
    upcoming = await add_window(
        engine,
        owner,
        check_id=check_id,
        starts_at=text("now() + interval '1 hour'"),
        ends_at=text("now() + interval '2 hours'"),
    )
    await add_window(engine, await user_id(bob), check_id=(await create_check(bob))["id"])

    listed = (await alice.get("/api/maintenance")).json()
    assert [(w["id"], w["status"]) for w in listed] == [
        (upcoming, "upcoming"),
        (active, "active"),
        (past, "past"),
    ]
    for status, expected in [("active", active), ("upcoming", upcoming), ("past", past)]:
        windows = (await alice.get("/api/maintenance", params={"status": status})).json()
        assert [w["id"] for w in windows] == [expected]
    assert (await alice.get("/api/maintenance", params={"status": "x"})).status_code == 422


async def test_windows_of_deleted_checks_are_hidden(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    window_id = await add_window(engine, await user_id(alice), check_id=check["id"])
    await alice.delete(f"/api/checks/{check['id']}")

    assert (await alice.get("/api/maintenance")).json() == []
    assert (await alice.delete(f"/api/maintenance/{window_id}")).status_code == 404


# ---------- Edit, end early, delete ----------


async def test_end_early_with_a_past_time(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    """A browser clock running behind sends a time that is already past: it means now."""
    window_id = await add_window(engine, await user_id(alice), check_id=check["id"])
    await add_incident(engine, check["id"], down_alert_queued=False)  # DOWN held back
    assert await sweep(engine) == 0
    t = await now(engine)

    response = await alice.patch(
        f"/api/maintenance/{window_id}", json={"ends_at": iso(t - timedelta(seconds=30))}
    )

    assert response.status_code == 200
    window = response.json()
    assert window["status"] == "past"
    assert t <= datetime.fromisoformat(window["ends_at"]) < t + timedelta(seconds=5)
    assert (await alice.get(f"/api/checks/{check['id']}")).json()["in_maintenance"] is False
    assert await sweep(engine) == 1  # still down: the DOWN email goes out now

    again = await alice.patch(f"/api/maintenance/{window_id}", json={"reason": "late"})
    assert (again.status_code, again.json()) == (409, {"detail": "window has ended"})


async def test_extend_an_active_window(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    window_id = await add_window(engine, await user_id(alice), check_id=check["id"])
    t = await now(engine)

    response = await alice.patch(
        f"/api/maintenance/{window_id}", json={"ends_at": iso(t + 3 * HOUR)}
    )

    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["ends_at"]) == t + 3 * HOUR


async def test_start_can_change_only_before_the_window_starts(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    t = await now(engine)
    active = await create_window(
        alice, check_id=check["id"], starts_at=iso(t), ends_at=iso(t + HOUR)
    )
    upcoming = await create_window(
        alice, check_id=check["id"], starts_at=iso(t + HOUR), ends_at=iso(t + 2 * HOUR)
    )

    path = f"/api/maintenance/{active['id']}"
    response = await alice.patch(path, json={"starts_at": iso(t + HOUR / 2)})
    assert (response.status_code, response.json()) == (409, {"detail": "window has started"})
    # Sending back the unchanged start (a full form) is fine.
    unchanged = await alice.patch(path, json={"starts_at": active["starts_at"], "reason": "db"})
    assert unchanged.status_code == 200

    path = f"/api/maintenance/{upcoming['id']}"
    later = (await alice.patch(path, json={"starts_at": iso(t + HOUR * 1.5)})).json()
    assert datetime.fromisoformat(later["starts_at"]) == t + HOUR * 1.5
    started = (await alice.patch(path, json={"starts_at": iso(t - HOUR)})).json()
    assert started["status"] == "active"  # a past start means now


async def test_edit_validates(
    engine: AsyncEngine, alice: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    t = await now(engine)
    window = await create_window(
        alice,
        check_id=check["id"],
        starts_at=iso(t + HOUR),
        ends_at=iso(t + 2 * HOUR),
        reason="deploy",
    )
    path = f"/api/maintenance/{window['id']}"

    for body in [
        {"ends_at": iso(t + HOUR / 2)},  # before the start
        {"ends_at": iso(t - HOUR)},  # "now", still before the start
        {"starts_at": iso(t + 3 * HOUR)},  # after the end
        {"starts_at": None},
        {"ends_at": None},
        {"check_id": check["id"]},  # the target can't change
    ]:
        assert (await alice.patch(path, json=body)).status_code == 422, body
    assert (await alice.patch(path, json={"reason": None})).json()["reason"] is None


async def test_delete(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, check: dict[str, Any]
) -> None:
    window_id = await add_window(engine, await user_id(alice), check_id=check["id"])

    for method, body in [("PATCH", {"reason": "mine"}), ("DELETE", None)]:
        response = await bob.request(method, f"/api/maintenance/{window_id}", json=body)
        assert response.status_code == 404
    assert (await alice.delete(f"/api/maintenance/{window_id}")).status_code == 204
    assert (await alice.get("/api/maintenance")).json() == []
    assert (await alice.delete(f"/api/maintenance/{window_id}")).status_code == 404
