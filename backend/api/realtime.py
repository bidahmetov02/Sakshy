"""Realtime: Postgres NOTIFY → this process's LISTEN connection → SSE streams
(`design/sequences.md` (c)).

A notification only names a check (`core/events.py`). The hub marks the check dirty if an
open stream watches it, and every `FLUSH_S` reloads all dirty checks with the same queries
as REST, then pushes:

- to owner streams (`/api/events`): `check`, the check as `GET /api/checks/{id}` returns it,
  or `check_removed` with `{"id"}` once it is deleted;
- to public streams (`/api/public/{slug}/events`): `page`, the whole page as
  `GET /api/public/{slug}` returns it.

Every stream starts with `ready`: the client (re)fetches its snapshot then, so nothing falls
between the snapshot and the stream. Missed events are never replayed. A stream that can't
be kept exact is closed instead, and the client reconnects and refetches: when its queue
overflows (a slow client), a reload fails, the LISTEN connection was lost, the session
expires (owner streams) or the page is disabled or deleted (public streams). A public
stream follows the checks its page listed when it connected; page edits apply on reconnect.

Maintenance windows also start and end with no write: every `EDGE_S` the hub finds the
windows that crossed an edge since its last look and marks their checks dirty.
"""

import asyncio
import json
import logging
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from fastapi.sse import ServerSentEvent
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from api.queries import check_out, checks_with_state, db_now
from api.routes.public import build_public_page
from core.events import NOTIFY_CHANNEL
from core.models import Check, MaintenanceWindow, StatusPage

log = logging.getLogger(__name__)

FLUSH_S = 0.5  # how often dirty checks are reloaded: bursts within it cost one query
EDGE_S = 15.0  # how often maintenance window starts and ends are looked for
QUEUE_LIMIT = 256  # events waiting for one client before its stream is closed
RECONNECT_S = 2.0  # wait between attempts to re-open the LISTEN connection
LISTEN_APP_NAME = "sakshy-realtime"  # shows in pg_stat_activity
LISTEN_PING_S = 30.0  # an idle LISTEN connection is probed this often, to notice it's gone

READY = ServerSentEvent(event="ready", data={})  # data: the browser ignores an empty event


class Stream:
    """One open SSE connection: the events waiting for it, until it is closed."""

    def __init__(self, deadline: float | None = None) -> None:
        self._events: deque[ServerSentEvent] = deque([READY])
        self._wake = asyncio.Event()
        self._deadline = deadline  # event loop time at which the stream ends
        self.closed = False

    def push(self, event: ServerSentEvent) -> None:
        if self.closed:
            return
        if len(self._events) >= QUEUE_LIMIT:
            log.warning("closing an SSE stream: the client is not keeping up")
            self.close()
            return
        self._events.append(event)
        self._wake.set()

    def close(self) -> None:
        self.closed = True
        self._wake.set()

    async def __aiter__(self) -> AsyncIterator[ServerSentEvent]:
        while not self.closed:
            if self._events:
                yield self._events.popleft()
                continue
            self._wake.clear()
            try:
                async with asyncio.timeout_at(self._deadline):
                    await self._wake.wait()
            except TimeoutError:
                return


def _discard[K](index: dict[K, set[Stream]], key: K, stream: Stream) -> None:
    streams = index.get(key)
    if streams is not None:
        streams.discard(stream)
        if not streams:
            del index[key]


class Hub:
    """The open streams of this API process, fed from its LISTEN connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        url = engine.url.set(drivername="postgresql")  # asyncpg takes a plain libpq URL
        self._dsn = url.render_as_string(hide_password=False)
        self._owners: dict[int, set[Stream]] = {}
        self._pages: dict[int, set[Stream]] = {}
        self._check_pages: dict[int, Counter[int]] = {}  # check id → page id → streams
        self._dirty_checks: dict[int, int] = {}  # check id → owner id
        self._dirty_pages: set[int] = set()
        self._edges_since: datetime | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.listening = asyncio.Event()

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        """Waits briefly for the LISTEN connection; if the database is down, keeps retrying
        in the background."""
        self._tasks = [
            asyncio.create_task(self._listen(), name="realtime-listen"),
            asyncio.create_task(self._every(FLUSH_S, self.flush), name="realtime-flush"),
            asyncio.create_task(self._every(EDGE_S, self.scan_edges), name="realtime-edges"),
        ]
        with suppress(TimeoutError):
            await asyncio.wait_for(self.listening.wait(), timeout=5)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._close_all()

    # ---------- Streams ----------

    @contextmanager
    def owner_stream(self, owner_id: int, expires_at: datetime) -> Generator[Stream]:
        """The owner's checks, until `expires_at` (the session's end)."""
        left = (expires_at - datetime.now(UTC)).total_seconds()
        stream = Stream(deadline=asyncio.get_running_loop().time() + left)
        self._owners.setdefault(owner_id, set()).add(stream)
        try:
            yield stream
        finally:
            _discard(self._owners, owner_id, stream)

    @contextmanager
    def page_stream(self, page_id: int, check_ids: Collection[int]) -> Generator[Stream]:
        """A public page, following the checks it lists now."""
        stream = Stream()
        self._pages.setdefault(page_id, set()).add(stream)
        for check_id in check_ids:
            self._check_pages.setdefault(check_id, Counter())[page_id] += 1
        try:
            yield stream
        finally:
            _discard(self._pages, page_id, stream)
            for check_id in check_ids:
                pages = self._check_pages[check_id]
                pages[page_id] -= 1
                if pages[page_id] <= 0:
                    del pages[page_id]
                if not pages:
                    del self._check_pages[check_id]

    def _close_all(self) -> None:
        for streams in (*self._owners.values(), *self._pages.values()):
            for stream in streams:
                stream.close()

    def _close(self, index: dict[int, set[Stream]], key: int) -> None:
        for stream in index.get(key, ()):
            stream.close()

    # ---------- Events ----------

    def mark(self, check_id: int, owner_id: int) -> None:
        """The check changed: reload it at the next flush, if anyone is watching."""
        if owner_id in self._owners:
            self._dirty_checks[check_id] = owner_id
        self._dirty_pages.update(self._check_pages.get(check_id, ()))

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            event = json.loads(payload)
            self.mark(int(event["check_id"]), int(event["owner_id"]))
        except ValueError, KeyError, TypeError:
            log.warning("ignoring a malformed check event: %r", payload)

    async def flush(self) -> None:
        """Push every dirty check to its owner's streams and every dirty page to its
        streams. Streams that miss an update because a reload failed are closed."""
        checks, self._dirty_checks = self._dirty_checks, {}
        pages, self._dirty_pages = self._dirty_pages, set()
        if checks:
            try:
                await self._push_checks(checks)
            except Exception:
                log.exception("reloading checks for SSE failed")
                for owner_id in set(checks.values()):
                    self._close(self._owners, owner_id)
        for page_id in pages:
            try:
                await self._push_page(page_id)
            except Exception:
                log.exception("rebuilding status page %s for SSE failed", page_id)
                self._close(self._pages, page_id)

    async def _push_checks(self, checks: dict[int, int]) -> None:
        async with AsyncSession(self._engine) as session:
            rows = await session.execute(checks_with_state().where(Check.id.in_(checks)))
            found = [(row[0].owner_id, check_out(*row)) for row in rows]
        for owner_id, check in found:
            event = ServerSentEvent(event="check", raw_data=check.model_dump_json())
            for stream in self._owners.get(owner_id, ()):
                stream.push(event)
        live = {check.id for _, check in found}
        for check_id, owner_id in checks.items():
            if check_id not in live:  # deleted
                event = ServerSentEvent(event="check_removed", data={"id": check_id})
                for stream in self._owners.get(owner_id, ()):
                    stream.push(event)

    async def _push_page(self, page_id: int) -> None:
        if page_id not in self._pages:
            return
        async with AsyncSession(self._engine) as session:
            page = await session.get(StatusPage, page_id)
            if page is None or not page.is_enabled:
                self._close(self._pages, page_id)  # the reconnect gets a 404
                return
            body = await build_public_page(session, page)
        event = ServerSentEvent(event="page", raw_data=body.model_dump_json(exclude_unset=True))
        for stream in self._pages.get(page_id, ()):
            stream.push(event)

    async def scan_edges(self) -> None:
        """Mark the checks of maintenance windows that started or ended since the last scan
        (by the database clock)."""
        w = MaintenanceWindow
        async with AsyncSession(self._engine) as session:
            now = await db_now(session)
            since = self._edges_since
            if since is not None and (self._owners or self._check_pages):
                crossed = or_(
                    and_(w.starts_at > since, w.starts_at <= now),
                    and_(w.ends_at > since, w.ends_at <= now),
                )
                rows = await session.execute(
                    select(Check.id, Check.owner_id)
                    .distinct()
                    .join(w, or_(w.check_id == Check.id, w.group_id == Check.group_id))
                    .where(crossed, Check.deleted_at.is_(None))
                )
                for check_id, owner_id in rows:
                    self.mark(check_id, owner_id)
        self._edges_since = now

    # ---------- Background tasks ----------

    async def _every(self, seconds: float, job: Callable[[], Awaitable[None]]) -> None:
        while True:
            try:
                await job()
            except Exception:
                log.exception("realtime job failed")
            await asyncio.sleep(seconds)

    async def _listen(self) -> None:
        """Keep one LISTEN connection open. After a reconnect every stream is closed: events
        may have been missed while it was down, so clients refetch."""
        connected_before = False
        while True:
            conn: Any = None
            try:
                conn = await cast(Any, asyncpg).connect(  # asyncpg ships no types
                    self._dsn, server_settings={"application_name": LISTEN_APP_NAME}
                )
                await self._hold(conn, resync=connected_before)
                log.warning("LISTEN connection lost")
            except Exception as exc:
                log.warning("LISTEN connection failed: %r", exc)
            finally:
                self.listening.clear()
                if conn is not None:
                    connected_before = True
                    conn.terminate()
            await asyncio.sleep(RECONNECT_S)

    async def _hold(self, conn: Any, *, resync: bool) -> None:
        """Listen on `conn` until it is lost. Probing an idle connection notices a peer that
        vanished without closing it."""
        lost = asyncio.Event()

        def on_lost(_conn: object) -> None:
            lost.set()

        conn.add_termination_listener(on_lost)
        await conn.add_listener(NOTIFY_CHANNEL, self._on_notify)
        if resync:
            log.info("LISTEN connection restored; closing SSE streams to resync")
            self._close_all()
        self.listening.set()
        while not lost.is_set():
            try:
                await asyncio.wait_for(lost.wait(), timeout=LISTEN_PING_S)
            except TimeoutError:
                await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5)
