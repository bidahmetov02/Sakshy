"""Status pages: the owner's settings and items, and the public view by slug."""

from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import IncidentEndReason
from tests.conftest import create_check, user_id
from tests.factories import add_incident, add_rollup, add_window, sql


async def create_page(client: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/status-pages", json={"title": "Acme status"} | body)
    assert response.status_code == 201, response.text
    return response.json()


async def put_items(
    client: httpx2.AsyncClient, page_id: int, items: list[dict[str, Any]]
) -> httpx2.Response:
    return await client.put(f"/api/status-pages/{page_id}/items", json=items)


def item(check: dict[str, Any], name: str = "Website", position: int = 0) -> dict[str, Any]:
    return {"check_id": check["id"], "display_name": name, "position": position}


# ---------- Owner side ----------


async def test_create_list_get(alice: httpx2.AsyncClient, bob: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    await create_page(bob)

    assert len(page["slug"]) == 12
    assert {k: v for k, v in page.items() if k not in {"id", "slug", "created_at"}} == {
        "title": "Acme status",
        "is_enabled": True,
        "show_response_times": False,
        "show_uptime": True,
        "show_incidents": False,
        "items": [],
    }
    assert (await alice.get("/api/status-pages")).json() == [page]
    assert (await alice.get(f"/api/status-pages/{page['id']}")).json() == page
    assert (await create_page(alice))["slug"] != page["slug"]


async def test_patch(alice: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    path = f"/api/status-pages/{page['id']}"

    patched = (await alice.patch(path, json={"title": "New", "show_incidents": True})).json()

    assert (patched["title"], patched["show_incidents"], patched["slug"]) == (
        "New",
        True,
        page["slug"],
    )
    for body in [{"title": ""}, {"is_enabled": None}, {"slug": "mine"}]:
        assert (await alice.patch(path, json=body)).status_code == 422, body


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"title": "mine"}),
        ("DELETE", "", None),
        ("PUT", "/items", []),
    ],
)
async def test_another_users_page_is_404(
    alice: httpx2.AsyncClient,
    bob: httpx2.AsyncClient,
    method: str,
    path: str,
    body: Any,
) -> None:
    page = await create_page(alice)

    response = await bob.request(method, f"/api/status-pages/{page['id']}{path}", json=body)

    assert (response.status_code, response.json()) == (404, {"detail": "status page not found"})


async def test_delete(alice: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    await put_items(alice, page["id"], [item(await create_check(alice))])

    assert (await alice.delete(f"/api/status-pages/{page['id']}")).status_code == 204
    assert (await alice.get("/api/status-pages")).json() == []
    assert (await alice.get(f"/api/public/{page['slug']}")).status_code == 404


async def test_put_items_replaces_the_list(alice: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    api, web = await create_check(alice), await create_check(alice)

    first = (await put_items(alice, page["id"], [item(web, "Web", 1), item(api, "API", 0)])).json()
    assert [(i["display_name"], i["position"]) for i in first] == [("API", 0), ("Web", 1)]

    # Equal positions keep the list's order (ties by id).
    second = (await put_items(alice, page["id"], [item(web, "Web"), item(api, "API")])).json()
    assert [i["display_name"] for i in second] == ["Web", "API"]
    assert {i["id"] for i in second}.isdisjoint(i["id"] for i in first)  # new ids
    assert (await alice.get(f"/api/status-pages/{page['id']}")).json()["items"] == second

    assert (await put_items(alice, page["id"], [])).json() == []


async def test_put_items_validates(alice: httpx2.AsyncClient, bob: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    mine = await create_check(alice)
    deleted = await create_check(alice)
    await alice.delete(f"/api/checks/{deleted['id']}")
    bobs = await create_check(bob)

    for items, loc in [
        ([item(mine), item(mine)], ["body", 1, "check_id"]),
        ([item(mine), item(bobs)], ["body", 1, "check_id"]),
        ([item(deleted)], ["body", 0, "check_id"]),
        ([item(mine, " ")], ["body", 0, "display_name"]),
        ([item(mine, position=-1)], ["body", 0, "position"]),
    ]:
        response = await put_items(alice, page["id"], items)
        assert response.status_code == 422, items
        assert response.json()["detail"][0]["loc"] == loc
    assert (await put_items(alice, page["id"], [item(mine)] * 101)).status_code == 422
    assert (await alice.get(f"/api/status-pages/{page['id']}")).json()["items"] == []


async def test_items_of_deleted_checks_are_hidden(alice: httpx2.AsyncClient) -> None:
    page = await create_page(alice)
    kept, deleted = await create_check(alice), await create_check(alice)
    await put_items(alice, page["id"], [item(kept, "Kept"), item(deleted, "Gone")])

    await alice.delete(f"/api/checks/{deleted['id']}")

    owner_view = (await alice.get(f"/api/status-pages/{page['id']}")).json()
    assert [i["display_name"] for i in owner_view["items"]] == ["Kept"]
    public = (await alice.get(f"/api/public/{page['slug']}")).json()
    assert [i["display_name"] for i in public["items"]] == ["Kept"]


# ---------- Public ----------


@pytest.fixture
async def page(alice: httpx2.AsyncClient) -> dict[str, Any]:
    return await create_page(alice)


async def public(
    anon: httpx2.AsyncClient, page: dict[str, Any], path: str = "", **params: Any
) -> httpx2.Response:
    return await anon.get(f"/api/public/{page['slug']}{path}", params=params)


async def test_public_page_needs_no_auth_and_hides_private_fields(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    check = await create_check(alice)
    await put_items(alice, page["id"], [item(check, "Website")])

    response = await public(anon, page)

    assert response.status_code == 200
    body = response.json()
    [entry] = body["items"]
    assert body.keys() == {"title", "overall_status", "items"}  # incidents off by default
    # uptime on by default, response times off
    assert entry.keys() == {"id", "display_name", "state", "since", "uptime_pct"}
    assert (entry["display_name"], entry["state"], entry["uptime_pct"]) == (
        "Website",
        "pending",
        None,
    )
    assert check["url"] not in response.text


async def test_unknown_or_disabled_page_is_404(
    alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    await alice.patch(f"/api/status-pages/{page['id']}", json={"is_enabled": False})

    for slug in (page["slug"], "no-such-page"):
        response = await anon.get(f"/api/public/{slug}")
        assert (response.status_code, response.json()) == (404, {"detail": "status page not found"})


async def test_public_states_and_overall_status(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    stored = {  # display name -> stored state
        "up": "up",
        "failing": "failing",
        "down": "down",
        "pending": "pending",
        "paused": "up",
        "maintenance": "up",
        "down-in-window": "down",
    }
    checks = {name: await create_check(alice) for name in stored}
    for name, check in checks.items():
        await sql(
            engine,
            "UPDATE check_state SET state = :s WHERE check_id = :id",
            s=stored[name],
            id=check["id"],
        )
    incident = await add_incident(engine, checks["down"]["id"])  # started 10 min ago
    await alice.post(f"/api/checks/{checks['paused']['id']}/pause")
    owner = await user_id(alice)
    for name in ("maintenance", "down-in-window"):
        await add_window(engine, owner, check_id=checks[name]["id"])
    await put_items(
        alice, page["id"], [item(c, name, i) for i, (name, c) in enumerate(checks.items())]
    )

    body = (await public(anon, page)).json()

    states = {i["display_name"]: i["state"] for i in body["items"]}
    assert states == {
        "up": "up",
        "failing": "up",  # not confirmed: no incident
        "down": "down",
        "pending": "pending",
        "paused": "paused",
        "maintenance": "maintenance",
        "down-in-window": "maintenance",  # maintenance wins
    }
    since = {i["display_name"]: i["since"] for i in body["items"]}
    async with engine.connect() as conn:
        started, window_start = (
            await conn.execute(
                text(
                    "SELECT (SELECT started_at FROM incidents WHERE id = :i),"
                    " (SELECT min(starts_at) FROM maintenance_windows)"
                ),
                {"i": incident},
            )
        ).one()
    assert since["down"] == started.isoformat().replace("+00:00", "Z")
    assert since["maintenance"] == window_start.isoformat().replace("+00:00", "Z")
    # 4 checks count (up, failing, down, pending); one of them is down
    assert body["overall_status"] == "partial_outage"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["up", "pending"], "operational"),
        (["down", "up"], "partial_outage"),
        (["down", "down"], "major_outage"),
        ([], "operational"),
    ],
)
async def test_overall_status_tiers(
    engine: AsyncEngine,
    alice: httpx2.AsyncClient,
    anon: httpx2.AsyncClient,
    page: dict[str, Any],
    states: list[str],
    expected: str,
) -> None:
    items: list[dict[str, Any]] = []
    for i, state in enumerate(states):
        check = await create_check(alice)
        await sql(
            engine,
            "UPDATE check_state SET state = :s WHERE check_id = :id",
            s=state,
            id=check["id"],
        )
        items.append(item(check, f"c{i}"))
    await put_items(alice, page["id"], items)

    assert (await public(anon, page)).json()["overall_status"] == expected


async def test_flags_choose_the_optional_fields(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    check = await create_check(alice)
    await sql(
        engine, "UPDATE check_state SET last_response_ms = 87 WHERE check_id = :id", id=check["id"]
    )
    await add_rollup(engine, check["id"], hours_ago=1, total=100, ok=99, rt_ms=[80])
    await add_rollup(engine, check["id"], hours_ago=800, total=100, ok=0, rt_ms=[])  # > 30 days
    await put_items(alice, page["id"], [item(check)])
    flags = {"show_uptime": False, "show_response_times": True}
    await alice.patch(f"/api/status-pages/{page['id']}", json=flags)

    [entry] = (await public(anon, page)).json()["items"]
    assert "uptime_pct" not in entry
    assert entry["last_response_ms"] == 87

    await alice.patch(f"/api/status-pages/{page['id']}", json={"show_uptime": True})
    [entry] = (await public(anon, page)).json()["items"]
    assert entry["uptime_pct"] == pytest.approx(99)  # last 30 days only


async def test_public_incidents(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    listed, unlisted = await create_check(alice), await create_check(alice)
    await add_incident(engine, listed["id"])  # open
    await add_incident(
        engine,
        listed["id"],
        started_at=text("now() - interval '40 days'"),
        detected_at=text("now() - interval '40 days'"),
        ended_at=text("now() - interval '31 days'"),  # ended before the last 30 days
        end_reason=IncidentEndReason.RECOVERED,
    )
    await add_incident(engine, unlisted["id"])
    [entry] = (await put_items(alice, page["id"], [item(listed, "Website")])).json()
    await alice.patch(f"/api/status-pages/{page['id']}", json={"show_incidents": True})

    [incident] = (await public(anon, page)).json()["incidents"]

    assert incident.keys() == {"item_id", "display_name", "started_at", "ended_at"}
    assert (incident["item_id"], incident["display_name"], incident["ended_at"]) == (
        entry["id"],
        "Website",
        None,
    )


async def test_public_stats(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    check = await create_check(alice)
    await add_rollup(engine, check["id"], hours_ago=0, total=10, ok=9, rt_ms=[100])
    [entry] = (await put_items(alice, page["id"], [item(check)])).json()
    path = f"/api/status-pages/{page['id']}"

    # uptime only (the default)
    stats = (await public(anon, page, "/stats", item=entry["id"], range="week")).json()
    assert stats["uptime_pct"] == pytest.approx(90)
    assert stats["buckets"][-1].keys() == {"t", "total", "ok"}

    await alice.patch(path, json={"show_uptime": False, "show_response_times": True})
    stats = (await public(anon, page, "/stats", item=entry["id"], range="week")).json()
    assert stats.keys() == {"buckets"}
    assert stats["buckets"][-1] | {"t": None} == {"t": None, "avg_ms": 100, "max_ms": 100}

    await alice.patch(path, json={"show_response_times": False})
    assert (await public(anon, page, "/stats", item=entry["id"])).status_code == 404


async def test_public_stats_only_for_the_pages_own_items(
    alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, page: dict[str, Any]
) -> None:
    other_page = await create_page(alice)
    check = await create_check(alice)
    [other] = (await put_items(alice, other_page["id"], [item(check)])).json()

    for item_id in (other["id"], 999_999):
        response = await public(anon, page, "/stats", item=item_id)
        assert (response.status_code, response.json()) == (404, {"detail": "item not found"})
    assert (await public(anon, page, "/stats")).status_code == 422  # item is required
