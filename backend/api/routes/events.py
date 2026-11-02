"""SSE streams (`api/realtime.py`): the signed-in user's checks, and a public page.

Neither holds a database connection while it is open. Comment pings every 15 s keep
proxies from closing an idle stream.
"""

from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Claims
from api.realtime import Hub
from api.routes.public import find_page
from core.models import StatusPageItem

router = APIRouter(tags=["realtime"])


def _hub(request: Request) -> Hub:
    return request.app.state.hub


@router.get("/events", response_class=EventSourceResponse)
async def events(request: Request, claims: Claims) -> AsyncIterable[ServerSentEvent]:
    """`ready` first: fetch the snapshot then. After it, `check` (the check as
    `GET /checks/{id}` returns it) and `check_removed` (`{"id"}`). Ends when the session
    expires."""
    with _hub(request).owner_stream(claims.user_id, claims.expires_at) as stream:
        async for event in stream:
            yield event


async def _watched_page(slug: str, request: Request) -> tuple[int, list[int]]:
    """The page and the checks it lists, or a 404 before the stream starts. A session of
    its own, closed before streaming, rather than the request's."""
    async with AsyncSession(request.app.state.engine) as session:
        page = await find_page(session, slug)
        check_ids = await session.scalars(
            select(StatusPageItem.check_id).where(StatusPageItem.status_page_id == page.id)
        )
        return page.id, list(check_ids)


@router.get("/public/{slug}/events", response_class=EventSourceResponse, tags=["public"])
async def public_events(
    request: Request, watched: Annotated[tuple[int, list[int]], Depends(_watched_page)]
) -> AsyncIterable[ServerSentEvent]:
    """`ready` first: fetch the page then. After it, `page` (the page as `GET /public/{slug}`
    returns it) whenever one of its checks changes. Item and flag edits apply on reconnect;
    the stream ends if the page is disabled or deleted."""
    page_id, check_ids = watched
    with _hub(request).page_stream(page_id, check_ids) as stream:
        async for event in stream:
            yield event
