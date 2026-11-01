"""Checks CRUD, pause/resume/run and ownership over HTTP, against real Postgres.

Runtime changes follow the lock-order rule (`design/sequences.md` (f)).
"""

import asyncio
from typing import Any

import httpx2
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import (
    Check,
    ErrorKind,
    Incident,
    IncidentEndReason,
    MonitorState,
    Notification,
)
from tests.conftest import CHECK, create_check, user_id
from tests.factories import add_group, add_incident, rows, sql, state_of
from worker.probe import ProbeResult
from worker.runs import Completion, claim, complete

OK = ProbeResult(ok=True, response_ms=120, status_code=200)


async def due_in(engine: AsyncEngine, check_id: int) -> float:
    """Seconds until the check is due, on the database clock."""
    async with engine.connect() as conn:
        stmt = (
            "SELECT extract(epoch FROM next_run_at - now()) FROM check_state WHERE check_id = :id"
        )
        return float((await conn.execute(text(stmt), {"id": check_id})).scalar_one())


# ---------- Create, read ----------


async def test_create_returns_the_check_with_live_state(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)

    volatile = {"id", "created_at", "updated_at", "state_changed_at", "next_run_at"}
    assert {k: v for k, v in check.items() if k not in volatile} == CHECK | {
        "timeout_s": 10.0,
        "expected_status": 200,
        "expected_body": None,
        "group_id": None,
        "effective_alert_emails": ["alice@x.io"],
        "is_paused": False,
        "state": "pending",
        "last_checked_at": None,
        "last_ok": None,
        "last_status_code": None,
        "last_response_ms": None,
        "last_error_kind": None,
        "last_error_message": None,
        "incident_started_at": None,
        "in_maintenance": False,
    }
    assert await due_in(engine, check["id"]) <= 0  # first run right away
    assert (await alice.get(f"/api/checks/{check['id']}")).json() == check


async def test_grouped_check_alerts_the_groups_emails(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    group_id = await add_group(engine, await user_id(alice), alert_emails=["ops@x.io"])
    check = await create_check(alice, group_id=group_id)

    assert check["alert_emails"] == ["alice@x.io"]
    assert check["effective_alert_emails"] == ["ops@x.io"]


async def test_alert_emails_are_deduplicated_ignoring_case(alice: httpx2.AsyncClient) -> None:
    check = await create_check(alice, alert_emails=["a@x.io", "b@x.io", "A@x.io"])
    assert check["alert_emails"] == ["a@x.io", "b@x.io"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_s": 60},  # not below the interval
        {"timeout_s": 31, "interval_s": 120},  # above the 30 s cap
        {"timeout_s": 0},
        {"interval_s": 29},
        {"interval_s": 3601},
        {"url": "ftp://example.com"},
        {"url": "example.com"},
        {"expected_status": 600},
        {"name": "  "},
        {"alert_emails": []},
        {"alert_emails": ["not-an-email"]},
        {"alert_emails": [f"u{i}@x.io" for i in range(21)]},
        {"unknown_field": 1},
    ],
)
async def test_create_validates_config(
    alice: httpx2.AsyncClient, overrides: dict[str, Any]
) -> None:
    response = await alice.post("/api/checks", json=CHECK | overrides)
    assert response.status_code == 422, overrides


async def test_cannot_use_another_users_group(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    bobs_group = await add_group(engine, await user_id(bob))

    for group_id in (bobs_group, 999_999):
        response = await alice.post("/api/checks", json=CHECK | {"group_id": group_id})
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "group_id"]


async def test_list_shows_own_live_checks_with_open_incident(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    group_id = await add_group(engine, await user_id(alice))
    first = await create_check(alice, name="first")
    second = await create_check(alice, name="second", group_id=group_id)
    deleted = await create_check(alice, name="deleted")
    await alice.delete(f"/api/checks/{deleted['id']}")
    await create_check(bob, name="bob's")
    await add_incident(engine, first["id"])
    await sql(engine, "UPDATE check_state SET state = 'down' WHERE check_id = :id", id=first["id"])

    checks = (await alice.get("/api/checks")).json()

    assert [c["name"] for c in checks] == ["first", "second"]
    assert checks[0]["state"] == "down"
    assert checks[0]["incident_started_at"] is not None
    assert checks[1]["incident_started_at"] is None
    grouped = (await alice.get("/api/checks", params={"group_id": group_id})).json()
    assert [c["id"] for c in grouped] == [second["id"]]


# ---------- Ownership ----------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"name": "mine now"}),
        ("DELETE", "", None),
        ("POST", "/pause", None),
        ("POST", "/resume", None),
        ("POST", "/run", None),
    ],
)
async def test_another_users_check_is_404(
    engine: AsyncEngine,
    alice: httpx2.AsyncClient,
    bob: httpx2.AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    check = await create_check(alice)

    response = await bob.request(method, f"/api/checks/{check['id']}{path}", json=body)

    assert response.status_code == 404
    assert response.json() == {"detail": "check not found"}
    [stored] = await rows(engine, Check)
    assert (stored.name, stored.is_paused, stored.deleted_at) == ("site", False, None)


async def test_deleted_check_is_404(alice: httpx2.AsyncClient) -> None:
    check = await create_check(alice)
    assert (await alice.delete(f"/api/checks/{check['id']}")).status_code == 204

    for method, path in [("GET", ""), ("DELETE", ""), ("POST", "/pause"), ("POST", "/run")]:
        response = await alice.request(method, f"/api/checks/{check['id']}{path}")
        assert response.status_code == 404, (method, path)


# ---------- Update ----------


async def test_patch_changes_config_and_keeps_state(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)
    await add_incident(engine, check["id"])
    await sql(engine, "UPDATE check_state SET state = 'down' WHERE check_id = :id", id=check["id"])

    response = await alice.patch(
        f"/api/checks/{check['id']}",
        json={"url": "https://example.com/fixed", "expected_body": "ok"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["url"] == "https://example.com/fixed"
    assert updated["expected_body"] == "ok"
    assert updated["name"] == "site"
    assert updated["state"] == "down"
    assert updated["incident_started_at"] is not None  # still down, incident still open
    assert updated["updated_at"] > check["updated_at"]


async def test_patch_validates_the_merged_config(alice: httpx2.AsyncClient) -> None:
    check = await create_check(alice, interval_s=60, timeout_s=10)
    path = f"/api/checks/{check['id']}"

    assert (await alice.patch(path, json={"interval_s": 30, "timeout_s": 30})).status_code == 422
    assert (await alice.patch(path, json={"name": None})).status_code == 422
    assert (await alice.patch(path, json={"bogus": 1})).status_code == 422
    assert (await alice.patch(path, json={"interval_s": 30})).status_code == 200


async def test_patch_moves_between_groups_and_ungroups(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    group_id = await add_group(engine, await user_id(alice), alert_emails=["ops@x.io"])
    bobs_group = await add_group(engine, await user_id(bob))
    check = await create_check(alice)
    path = f"/api/checks/{check['id']}"

    grouped = (await alice.patch(path, json={"group_id": group_id})).json()
    assert grouped["effective_alert_emails"] == ["ops@x.io"]
    assert (await alice.patch(path, json={"group_id": bobs_group})).status_code == 422
    ungrouped = (await alice.patch(path, json={"group_id": None})).json()
    assert (ungrouped["group_id"], ungrouped["effective_alert_emails"]) == (None, ["alice@x.io"])


@pytest.mark.parametrize(
    ("due", "new_interval", "expected"),
    [
        (50, 120, 110),  # longer: shifted later by the difference
        (50, 30, 20),  # shorter: shifted earlier
        (20, 30, 0),  # shorter, and the shifted slot already passed: due now
    ],
)
async def test_interval_change_shifts_next_run(
    engine: AsyncEngine, alice: httpx2.AsyncClient, due: int, new_interval: int, expected: int
) -> None:
    check = await create_check(alice, interval_s=60, timeout_s=5)
    await sql(
        engine,
        "UPDATE check_state SET next_run_at = now() + make_interval(secs => :s)"
        " WHERE check_id = :id",
        s=due,
        id=check["id"],
    )

    await alice.patch(f"/api/checks/{check['id']}", json={"interval_s": new_interval})

    assert await due_in(engine, check["id"]) == pytest.approx(expected, abs=2)


@pytest.mark.parametrize(
    "runtime",
    [
        "lease_owner = 'w1', lease_until = now() + interval '15 s'",  # running
        "state = 'failing', consecutive_failures = 1, first_failure_at = now()",  # retrying
    ],
)
async def test_interval_change_leaves_running_or_retrying_checks_alone(
    engine: AsyncEngine, alice: httpx2.AsyncClient, runtime: str
) -> None:
    check = await create_check(alice)
    await sql(
        engine,
        f"UPDATE check_state SET next_run_at = now() + interval '50 s', {runtime}"
        " WHERE check_id = :id",
        id=check["id"],
    )

    await alice.patch(f"/api/checks/{check['id']}", json={"interval_s": 600})

    assert await due_in(engine, check["id"]) == pytest.approx(50, abs=2)


# ---------- Pause, resume, delete ----------


async def test_pause_ends_the_incident_without_an_up_email(
    engine: AsyncEngine, alice: httpx2.AsyncClient, listener: asyncio.Queue[dict[str, Any]]
) -> None:
    check = await create_check(alice)
    await add_incident(engine, check["id"])
    await sql(
        engine,
        "UPDATE check_state SET state = 'down', consecutive_failures = 3,"
        " first_failure_at = now(), last_response_ms = 80 WHERE check_id = :id",
        id=check["id"],
    )

    assert (await alice.post(f"/api/checks/{check['id']}/pause")).status_code == 204

    paused = (await alice.get(f"/api/checks/{check['id']}")).json()
    assert (paused["is_paused"], paused["state"], paused["incident_started_at"]) == (
        True,
        "pending",
        None,
    )
    state = await state_of(engine, check["id"])
    assert (state.consecutive_failures, state.first_failure_at) == (0, None)
    [incident] = await rows(engine, Incident)
    assert incident.end_reason is IncidentEndReason.PAUSED
    assert await rows(engine, Notification) == []
    event = await asyncio.wait_for(listener.get(), timeout=2)
    assert event == {
        "check_id": check["id"],
        "owner_id": await user_id(alice),
        "state": "pending",
        "rt_ms": 80,
    }


async def test_pause_and_resume_are_idempotent(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)
    path = f"/api/checks/{check['id']}"

    assert (await alice.post(f"{path}/resume")).status_code == 204  # not paused: no-op
    for _ in range(2):
        assert (await alice.post(f"{path}/pause")).status_code == 204
    assert await claim(engine, "w1", 10) == []  # paused checks are never claimed

    await sql(
        engine,
        "UPDATE check_state SET next_run_at = now() + interval '1 hour' WHERE check_id = :id",
        id=check["id"],
    )
    for _ in range(2):
        assert (await alice.post(f"{path}/resume")).status_code == 204
    assert (await alice.get(path)).json()["is_paused"] is False
    assert await due_in(engine, check["id"]) <= 0  # resume runs it right away
    assert [c.check_id for c in await claim(engine, "w1", 10)] == [check["id"]]


async def test_delete_soft_deletes_and_ends_the_incident(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)
    await add_incident(engine, check["id"])

    assert (await alice.delete(f"/api/checks/{check['id']}")).status_code == 204

    [stored] = await rows(engine, Check)
    assert stored.deleted_at is not None
    [incident] = await rows(engine, Incident)
    assert incident.end_reason is IncidentEndReason.CHECK_DELETED
    assert await rows(engine, Notification) == []
    assert await claim(engine, "w1", 10) == []


async def test_pause_discards_a_run_in_flight(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)
    [c] = await claim(engine, "w1", 10)

    await alice.post(f"/api/checks/{check['id']}/pause")

    assert await complete(engine, "w1", c, OK, c.claimed_at) is Completion.DISCARDED
    assert (await state_of(engine, check["id"])).lease_owner is None


async def test_pause_waiting_on_a_completion_closes_the_incident_it_opened(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    """The worker locks first and opens an incident; the pause, blocked on the lock, sees
    it once the worker commits and closes it."""
    check = await create_check(alice)
    async with engine.connect() as worker:
        tx = await worker.begin()
        await worker.execute(
            text("SELECT 1 FROM check_state WHERE check_id = :id FOR UPDATE"), {"id": check["id"]}
        )
        pause = asyncio.create_task(alice.post(f"/api/checks/{check['id']}/pause"))
        await asyncio.sleep(0.3)
        assert not pause.done()  # blocked on the check_state row lock

        await worker.execute(
            insert(Incident).values(
                check_id=check["id"],
                started_at=text("now()"),
                detected_at=text("now()"),
                cause_kind=ErrorKind.TIMEOUT,
                down_alert_queued=True,
            )
        )
        await worker.execute(
            text("UPDATE check_state SET state = 'down' WHERE check_id = :id"), {"id": check["id"]}
        )
        await tx.commit()

    assert (await pause).status_code == 204
    [incident] = await rows(engine, Incident)
    assert incident.end_reason is IncidentEndReason.PAUSED
    assert (await state_of(engine, check["id"])).state is MonitorState.PENDING


# ---------- Run now ----------


async def test_run_makes_the_check_due_now(engine: AsyncEngine, alice: httpx2.AsyncClient) -> None:
    check = await create_check(alice)
    await sql(
        engine,
        "UPDATE check_state SET next_run_at = now() + interval '1 hour' WHERE check_id = :id",
        id=check["id"],
    )

    response = await alice.post(f"/api/checks/{check['id']}/run")

    assert response.status_code == 202
    assert await due_in(engine, check["id"]) <= 0


async def test_run_conflicts_while_running_or_paused(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    check = await create_check(alice)
    path = f"/api/checks/{check['id']}"
    await claim(engine, "w1", 10)

    response = await alice.post(f"{path}/run")
    assert (response.status_code, response.json()) == (409, {"detail": "check is running"})

    await sql(
        engine,
        "UPDATE check_state SET lease_until = now() - interval '1 s' WHERE check_id = :id",
        id=check["id"],
    )
    assert (await alice.post(f"{path}/run")).status_code == 202  # an expired lease is free

    await alice.post(f"{path}/pause")
    response = await alice.post(f"{path}/run")
    assert (response.status_code, response.json()) == (409, {"detail": "check is paused"})
