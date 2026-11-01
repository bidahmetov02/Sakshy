"""Status pages, owner side: the page's settings and the checks it lists.

Another user's page is a 404. The slug is random and unguessable; the public view is in
`api/routes/public.py`.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import Field
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Session, UserId
from api.queries import field_error
from api.schemas import (
    MAX_PAGE_ITEMS,
    ItemIn,
    ItemOut,
    StatusPageIn,
    StatusPageOut,
    StatusPagePatch,
)
from core.models import Check, StatusPage, StatusPageItem

router = APIRouter(prefix="/status-pages", tags=["status pages"])

SLUG_BYTES = 9  # 12 URL-safe characters


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "status page not found")


async def _items(session: AsyncSession, page_ids: list[int]) -> dict[int, list[ItemOut]]:
    """Each page's items in display order. Items of deleted checks are left out."""
    rows = await session.scalars(
        select(StatusPageItem)
        .join(Check, Check.id == StatusPageItem.check_id)
        .where(StatusPageItem.status_page_id.in_(page_ids), Check.deleted_at.is_(None))
        .order_by(StatusPageItem.position, StatusPageItem.id)
    )
    items: dict[int, list[ItemOut]] = {page_id: [] for page_id in page_ids}
    for item in rows:
        items[item.status_page_id].append(ItemOut.model_validate(item, from_attributes=True))
    return items


def _out(page: StatusPage, items: list[ItemOut]) -> StatusPageOut:
    return StatusPageOut(
        id=page.id,
        slug=page.slug,
        title=page.title,
        is_enabled=page.is_enabled,
        show_response_times=page.show_response_times,
        show_uptime=page.show_uptime,
        show_incidents=page.show_incidents,
        created_at=page.created_at,
        items=items,
    )


async def _owned(
    session: AsyncSession, user_id: int, page_id: int, *, lock: bool = False
) -> StatusPage:
    stmt = select(StatusPage).where(StatusPage.id == page_id, StatusPage.owner_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    page = (await session.scalars(stmt.execution_options(populate_existing=True))).one_or_none()
    if page is None:
        raise _not_found()
    return page


async def _get(session: AsyncSession, user_id: int, page_id: int) -> StatusPageOut:
    page = await _owned(session, user_id, page_id)
    return _out(page, (await _items(session, [page.id]))[page.id])


@router.get("")
async def list_pages(user_id: UserId, session: Session) -> list[StatusPageOut]:
    pages = list(
        (
            await session.scalars(
                select(StatusPage).where(StatusPage.owner_id == user_id).order_by(StatusPage.id)
            )
        ).all()
    )
    items = await _items(session, [page.id for page in pages])
    return [_out(page, items[page.id]) for page in pages]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_page(body: StatusPageIn, user_id: UserId, session: Session) -> StatusPageOut:
    page = StatusPage(owner_id=user_id, slug=secrets.token_urlsafe(SLUG_BYTES), **body.model_dump())
    session.add(page)
    await session.commit()
    return await _get(session, user_id, page.id)


@router.get("/{page_id}")
async def get_page(page_id: int, user_id: UserId, session: Session) -> StatusPageOut:
    return await _get(session, user_id, page_id)


@router.patch("/{page_id}")
async def update_page(
    page_id: int, body: StatusPagePatch, user_id: UserId, session: Session
) -> StatusPageOut:
    page = await _owned(session, user_id, page_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(page, field, value)
    await session.commit()
    return await _get(session, user_id, page_id)


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(page_id: int, user_id: UserId, session: Session) -> None:
    page = await _owned(session, user_id, page_id)
    await session.execute(delete(StatusPage).where(StatusPage.id == page.id))
    await session.commit()


@router.put("/{page_id}/items")
async def replace_items(
    page_id: int,
    body: Annotated[list[ItemIn], Body(), Field(max_length=MAX_PAGE_ITEMS)],
    user_id: UserId,
    session: Session,
) -> list[ItemOut]:
    """Replaces the whole list. Items get new ids, so public chart links change too."""
    page = await _owned(session, user_id, page_id, lock=True)  # one replace at a time
    check_ids = [item.check_id for item in body]
    for index, check_id in enumerate(check_ids):
        if check_id in check_ids[:index]:
            raise field_error((index, "check_id"), "check listed twice", check_id)
    # KEY SHARE: the checks can't be purged before this commits.
    live = set(
        (
            await session.scalars(
                select(Check.id)
                .where(
                    Check.id.in_(check_ids),
                    Check.owner_id == user_id,
                    Check.deleted_at.is_(None),
                )
                .with_for_update(key_share=True)
            )
        ).all()
    )
    for index, check_id in enumerate(check_ids):
        if check_id not in live:
            raise field_error((index, "check_id"), "check not found", check_id)

    await session.execute(delete(StatusPageItem).where(StatusPageItem.status_page_id == page.id))
    if body:
        await session.execute(
            insert(StatusPageItem),
            [
                {"owner_id": user_id, "status_page_id": page.id, **item.model_dump()}
                for item in body
            ],
        )
    await session.commit()
    return (await _items(session, [page.id]))[page.id]
