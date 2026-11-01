"""Groups: CRUD, ownership, the computed status, and what deleting one does."""

from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import MaintenanceWindow
from tests.conftest import create_check, user_id
from tests.factories import add_incident, add_window, rows, sql
from worker.sweep import sweep

GROUP = {"name": "prod", "alert_emails": ["ops@x.io"]}


async def create_group(client: httpx2.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/groups", json=GROUP | overrides)
    assert response.status_code == 201, response.text
    return response.json()


async def set_state(engine: AsyncEngine, check_id: int, state: str) -> None:
    await sql(
        engine, "UPDATE check_state SET state = :s WHERE check_id = :id", s=state, id=check_id
    )


async def test_create_list_get(alice: httpx2.AsyncClient, bob: httpx2.AsyncClient) -> None:
    group = await create_group(alice)
    await create_group(alice, name="dev")
    await create_group(bob)

    assert {k: v for k, v in group.items() if k not in {"id", "created_at"}} == GROUP | {
        "status": "operational",
        "check_count": 0,
        "down_count": 0,
    }
    assert [g["name"] for g in (await alice.get("/api/groups")).json()] == ["dev", "prod"]
    assert (await alice.get(f"/api/groups/{group['id']}")).json() == group


async def test_patch(alice: httpx2.AsyncClient) -> None:
    group = await create_group(alice)
    path = f"/api/groups/{group['id']}"

    response = await alice.patch(path, json={"alert_emails": ["a@x.io", "b@x.io"]})
    assert response.status_code == 200
    assert response.json()["alert_emails"] == ["a@x.io", "b@x.io"]
    assert (await alice.patch(path, json={"name": "live"})).json()["name"] == "live"


@pytest.mark.parametrize(
    "body",
    [
        {"name": " "},
        {"name": None},
        {"alert_emails": []},
        {"alert_emails": None},
        {"alert_emails": ["nope"]},
        {"bogus": 1},
    ],
)
async def test_validation(alice: httpx2.AsyncClient, body: dict[str, Any]) -> None:
    group = await create_group(alice)
    assert (await alice.patch(f"/api/groups/{group['id']}", json=body)).status_code == 422
    assert (await alice.post("/api/groups", json=GROUP | body)).status_code == 422


async def test_names_are_unique_per_user(
    alice: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    await create_group(alice)
    dev = await create_group(alice, name="dev")
    await create_group(bob)  # another user may reuse it

    assert (await alice.post("/api/groups", json=GROUP)).status_code == 409
    response = await alice.patch(f"/api/groups/{dev['id']}", json={"name": "prod"})
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "body"), [("GET", None), ("PATCH", {"name": "mine"}), ("DELETE", None)]
)
async def test_another_users_group_is_404(
    alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, method: str, body: dict[str, Any] | None
) -> None:
    group = await create_group(alice)

    response = await bob.request(method, f"/api/groups/{group['id']}", json=body)

    assert (response.status_code, response.json()) == (404, {"detail": "group not found"})
    assert (await alice.get(f"/api/groups/{group['id']}")).json()["name"] == "prod"


# ---------- Status ----------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], "operational"),  # empty
        (["up", "up", "pending"], "operational"),
        (["up", "failing"], "operational"),  # failing isn't confirmed yet
        (["up", "down"], "partial_outage"),
        (["down", "down"], "major_outage"),
    ],
)
async def test_status_is_tiered(
    engine: AsyncEngine, alice: httpx2.AsyncClient, states: list[str], expected: str
) -> None:
    group = await create_group(alice)
    for state in states:
        check = await create_check(alice, group_id=group["id"])
        await set_state(engine, check["id"], state)

    status = (await alice.get(f"/api/groups/{group['id']}")).json()

    assert (status["status"], status["check_count"]) == (expected, len(states))
    assert status["down_count"] == states.count("down")


async def test_paused_and_maintenance_checks_dont_count(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    group = await create_group(alice)
    down = await create_check(alice, group_id=group["id"])
    paused = await create_check(alice, group_id=group["id"])
    in_window = await create_check(alice, group_id=group["id"])
    deleted = await create_check(alice, group_id=group["id"])
    for check in (down, paused, in_window, deleted):
        await set_state(engine, check["id"], "down")
    await alice.post(f"/api/checks/{paused['id']}/pause")  # back to pending
    await add_window(engine, await user_id(alice), check_id=in_window["id"])
    await alice.delete(f"/api/checks/{deleted['id']}")

    status = (await alice.get(f"/api/groups/{group['id']}")).json()

    # Only `down` counts: it is the only one, so this is a major outage.
    assert (status["status"], status["check_count"], status["down_count"]) == (
        "major_outage",
        3,
        1,
    )


async def test_group_maintenance_silences_its_status(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    group = await create_group(alice)
    check = await create_check(alice, group_id=group["id"])
    await set_state(engine, check["id"], "down")
    await add_window(engine, await user_id(alice), group_id=group["id"])

    assert (await alice.get(f"/api/groups/{group['id']}")).json()["status"] == "operational"


# ---------- Delete ----------


async def test_delete_ungroups_checks_and_drops_the_groups_windows(
    engine: AsyncEngine, alice: httpx2.AsyncClient
) -> None:
    group = await create_group(alice)
    check = await create_check(alice, group_id=group["id"])
    await add_window(engine, await user_id(alice), group_id=group["id"])
    await add_incident(engine, check["id"], down_alert_queued=False)  # suppressed DOWN

    assert (await alice.delete(f"/api/groups/{group['id']}")).status_code == 204

    after = (await alice.get(f"/api/checks/{check['id']}")).json()
    assert (after["group_id"], after["effective_alert_emails"]) == (None, ["alice@x.io"])
    assert after["in_maintenance"] is False
    assert await rows(engine, MaintenanceWindow) == []
    assert await sweep(engine) == 1  # no longer silenced: the DOWN email goes out
    assert (await alice.get(f"/api/groups/{group['id']}")).status_code == 404
    assert (await alice.delete(f"/api/groups/{group['id']}")).status_code == 404
