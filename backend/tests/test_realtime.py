"""Realtime: NOTIFY → the API's hub → SSE streams.

Streams are read over a real HTTP server: the in-process ASGI transport buffers whole
responses, so it can't stream.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.sse import ServerSentEvent
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncEngine

from api.realtime import QUEUE_LIMIT, Hub, Stream
from api.security import create_token
from core.settings import Settings
from tests.conftest import create_check, user_id
from tests.factories import add_window, sql
from tests.test_status_pages_api import create_page, item, put_items
from worker.probe import ProbeResult
from worker.runs import claim, complete

OK = ProbeResult(ok=True, response_ms=120, status_code=200)


@pytest.fixture
async def live(app: FastAPI) -> AsyncGenerator[str]:
    """The app served over HTTP on a free local port (its lifespan already runs)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, lifespan="off", log_level="warning", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110  (uvicorn only exposes a flag)
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    await task


@dataclass
class Event:
    name: str
    data: Any


class SSE:
    """One open stream, read in the background. Comment pings are skipped."""

    def __init__(self, response: httpx2.Response) -> None:
        self.status_code = response.status_code
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()  # None: the stream ended
        self._task = asyncio.create_task(self._read(response))

    async def _read(self, response: httpx2.Response) -> None:
        name, data = "message", list[str]()
        try:
            async for line in response.aiter_lines():
                if line == "":
                    if data:
                        self._events.put_nowait(Event(name, json.loads("\n".join(data))))
                    name, data = "message", []
                elif not line.startswith(":"):
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "event":
                        name = value
                    elif field == "data":
                        data.append(value)
        finally:
            self._events.put_nowait(None)

    async def next(self, within: float = 5) -> Event:
        event = await asyncio.wait_for(self._events.get(), within)
        assert event is not None, "the stream ended"
        return event

    async def ends(self, within: float = 5) -> None:
        while (event := await asyncio.wait_for(self._events.get(), within)) is not None:
            raise AssertionError(f"expected the stream to end, got {event}")

    async def quiet(self, seconds: float = 1.2) -> None:
        """Nothing arrives for longer than a flush."""
        await asyncio.sleep(seconds)
        assert self._events.empty(), self._events.get_nowait()

    async def stop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


@contextlib.asynccontextmanager
async def stream(url: str, token: str | None = None) -> AsyncGenerator[SSE]:
    headers = {"Cookie": f"access_token={token}"} if token else {}
    async with (
        httpx2.AsyncClient(timeout=10) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        sse = SSE(response)
        try:
            yield sse
        finally:
            await sse.stop()


def token_of(client: httpx2.AsyncClient) -> str:
    token = client.cookies.get("access_token")
    assert token is not None
    return token


@contextlib.asynccontextmanager
async def owner_stream(live: str, client: httpx2.AsyncClient) -> AsyncGenerator[SSE]:
    """The user's stream, past its `ready` event."""
    async with stream(f"{live}/api/events", token_of(client)) as sse:
        assert sse.status_code == 200
        assert await sse.next() == Event("ready", {})
        yield sse


async def run(engine: AsyncEngine, check_id: int, result: ProbeResult = OK) -> None:
    """One worker run of the check, completed as the worker does. Only it is made due."""
    await sql(
        engine,
        "UPDATE check_state SET next_run_at = CASE WHEN check_id = :id THEN now()"
        " ELSE now() + interval '1 hour' END",
        id=check_id,
    )
    [c] = await claim(engine, "w1", 100)
    await complete(engine, "w1", c, result, c.claimed_at)


# ---------- Owner stream ----------


async def test_owner_stream_needs_a_session(anon: httpx2.AsyncClient) -> None:
    assert (await anon.get("/api/events")).status_code == 401


async def test_a_run_pushes_the_check_as_rest_returns_it(
    engine: AsyncEngine, alice: httpx2.AsyncClient, live: str
) -> None:
    check = await create_check(alice)
    async with owner_stream(live, alice) as sse:
        await run(engine, check["id"])

        event = await sse.next()

        assert event.name == "check"
        assert event.data["state"] == "up"
        assert event.data == (await alice.get(f"/api/checks/{check['id']}")).json()


async def test_pause_resume_and_delete(
    engine: AsyncEngine, alice: httpx2.AsyncClient, live: str
) -> None:
    check = await create_check(alice)
    path = f"/api/checks/{check['id']}"
    async with owner_stream(live, alice) as sse:
        await alice.post(f"{path}/pause")
        event = await sse.next()
        assert (event.name, event.data["is_paused"]) == ("check", True)

        await alice.post(f"{path}/resume")
        event = await sse.next()
        assert (event.name, event.data["is_paused"]) == ("check", False)

        await alice.delete(path)
        assert await sse.next() == Event("check_removed", {"id": check["id"]})


async def test_create_and_update_push_the_check(alice: httpx2.AsyncClient, live: str) -> None:
    async with owner_stream(live, alice) as sse:
        check = await create_check(alice)
        assert await sse.next() == Event("check", check)

        renamed = (await alice.patch(f"/api/checks/{check['id']}", json={"name": "new"})).json()
        assert await sse.next() == Event("check", renamed)


async def test_only_the_owners_checks_are_pushed(
    engine: AsyncEngine, alice: httpx2.AsyncClient, bob: httpx2.AsyncClient, live: str
) -> None:
    bobs = await create_check(bob)
    async with owner_stream(live, alice) as sse:
        await run(engine, bobs["id"])
        await bob.post(f"/api/checks/{bobs['id']}/pause")
        await sse.quiet()


async def test_a_burst_is_pushed_once_per_check(alice: httpx2.AsyncClient, live: str) -> None:
    check = await create_check(alice)
    path = f"/api/checks/{check['id']}"
    async with owner_stream(live, alice) as sse:
        for name in ("a", "b", "c"):
            await alice.patch(path, json={"name": name})

        first = await sse.next()
        if first.data["name"] != "c":  # the flush may have split the burst once
            assert (await sse.next()).data["name"] == "c"
        await sse.quiet()


async def test_group_changes_push_its_checks(alice: httpx2.AsyncClient, live: str) -> None:
    body = {"name": "prod", "alert_emails": ["team@x.io"]}
    group = (await alice.post("/api/groups", json=body)).json()
    check = await create_check(alice, group_id=group["id"])
    async with owner_stream(live, alice) as sse:
        await alice.patch(f"/api/groups/{group['id']}", json={"alert_emails": ["ops@x.io"]})
        event = await sse.next()
        assert event.data["id"] == check["id"]
        assert event.data["effective_alert_emails"] == ["ops@x.io"]

        await alice.delete(f"/api/groups/{group['id']}")
        event = await sse.next()
        assert (event.data["group_id"], event.data["effective_alert_emails"]) == (
            None,
            check["alert_emails"],
        )


async def test_window_changes_push_the_covered_checks(alice: httpx2.AsyncClient, live: str) -> None:
    check = await create_check(alice)
    await create_check(alice, name="other")
    async with owner_stream(live, alice) as sse:
        window = {
            "check_id": check["id"],
            "starts_at": "2000-01-01T00:00:00Z",  # in the past: starts now
            "ends_at": "2999-01-01T00:00:00Z",
        }
        created = (await alice.post("/api/maintenance", json=window)).json()
        event = await sse.next()
        assert (event.data["id"], event.data["in_maintenance"]) == (check["id"], True)

        await alice.delete(f"/api/maintenance/{created['id']}")
        event = await sse.next()
        assert (event.data["id"], event.data["in_maintenance"]) == (check["id"], False)
        await sse.quiet()


async def test_windows_starting_and_ending_on_their_own_push_their_checks(
    engine: AsyncEngine, app: FastAPI, alice: httpx2.AsyncClient, live: str
) -> None:
    hub: Hub = app.state.hub
    check = await create_check(alice)
    async with owner_stream(live, alice) as sse:
        await hub.scan_edges()
        # Written directly, so nothing notifies: only the edge scan can notice.
        await add_window(
            engine,
            await user_id(alice),
            check_id=check["id"],
            starts_at=func.now(),
            ends_at=func.now() + text("interval '2 seconds'"),
        )
        await hub.scan_edges()
        event = await sse.next()
        assert (event.data["id"], event.data["in_maintenance"]) == (check["id"], True)

        await sql(engine, "SELECT pg_sleep(2)")
        await hub.scan_edges()
        while (event := await sse.next()).data["in_maintenance"]:
            pass  # the hub's own scan may have pushed the start again
        assert event.data["id"] == check["id"]


async def test_the_stream_ends_when_the_session_expires(
    alice: httpx2.AsyncClient, settings: Settings, live: str
) -> None:
    short = settings.model_copy(update={"access_token_ttl_s": 2})  # `exp` is whole seconds
    token = create_token(short, await user_id(alice))
    async with stream(f"{live}/api/events", token) as sse:
        assert await sse.next() == Event("ready", {})
        await sse.ends(within=4)


async def test_a_lost_listen_connection_closes_streams_to_resync(
    engine: AsyncEngine, alice: httpx2.AsyncClient, live: str
) -> None:
    check = await create_check(alice)
    async with owner_stream(live, alice) as sse:
        await sql(
            engine,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE application_name = 'sakshy-realtime'",
        )
        await sse.ends(within=8)  # closed once the hub has listened again

    async with owner_stream(live, alice) as sse:
        await run(engine, check["id"])
        assert (await sse.next()).name == "check"


async def test_a_slow_client_is_cut_off() -> None:
    stream_ = Stream()
    for _ in range(QUEUE_LIMIT + 1):
        stream_.push(ServerSentEvent(event="check", data={}))

    assert stream_.closed
    assert [event async for event in stream_] == []


# ---------- Public stream ----------


def public_stream(live: str, slug: str) -> contextlib.AbstractAsyncContextManager[SSE]:
    return stream(f"{live}/api/public/{slug}/events")


async def test_unknown_page_is_404(anon: httpx2.AsyncClient) -> None:
    assert (await anon.get("/api/public/nope/events")).status_code == 404


async def test_public_stream_pushes_the_page_as_rest_returns_it(
    engine: AsyncEngine, alice: httpx2.AsyncClient, anon: httpx2.AsyncClient, live: str
) -> None:
    page = await create_page(alice, show_incidents=True)
    listed = await create_check(alice)
    unlisted = await create_check(alice, name="other")
    await put_items(alice, page["id"], [item(listed, "Website")])
    async with public_stream(live, page["slug"]) as sse:
        assert await sse.next() == Event("ready", {})
        await run(engine, unlisted["id"])
        await sse.quiet()

        await run(engine, listed["id"])
        event = await sse.next()

        assert event.name == "page"
        assert event.data["items"][0]["state"] == "up"
        assert event.data == (await anon.get(f"/api/public/{page['slug']}")).json()
        assert "uptime_pct" in event.data["items"][0]  # allowed by the flags
        assert "last_response_ms" not in event.data["items"][0]  # left out, not null
        assert listed["url"] not in json.dumps(event.data)


async def test_public_stream_ends_once_the_page_is_disabled(
    engine: AsyncEngine, alice: httpx2.AsyncClient, live: str
) -> None:
    page = await create_page(alice)
    check = await create_check(alice)
    await put_items(alice, page["id"], [item(check)])
    async with public_stream(live, page["slug"]) as sse:
        assert await sse.next() == Event("ready", {})
        await alice.patch(f"/api/status-pages/{page['id']}", json={"is_enabled": False})
        await run(engine, check["id"])

        await sse.ends()
