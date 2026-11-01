"""Groups: a shared alert list for their checks, and a status computed over them.

Another user's group is a 404. Deleting a group ungroups its checks (the foreign key's
`SET NULL (group_id)`) and deletes its maintenance windows, so its checks stop being
silenced: the sweep then queues any suppressed DOWN email to each check's own list.
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import Select, and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import Session, UserId
from api.queries import counts_toward_status, tiered_status
from api.schemas import GroupIn, GroupOut, GroupPatch
from core.models import Check, CheckGroup, CheckState, MonitorState

router = APIRouter(prefix="/groups", tags=["groups"])


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "group not found")


def _duplicate() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "a group with this name already exists")


def _with_status(user_id: int) -> Select[CheckGroup, int, int, int]:
    """Each group with its live members counted: all of them, those that count toward the
    status, and those of these that are down."""
    counted = counts_toward_status()
    return (
        select(
            CheckGroup,
            func.count(Check.id),
            func.count(Check.id).filter(counted),
            func.count(Check.id).filter(counted, CheckState.state == MonitorState.DOWN),
        )
        .outerjoin(Check, and_(Check.group_id == CheckGroup.id, Check.deleted_at.is_(None)))
        .outerjoin(CheckState, CheckState.check_id == Check.id)
        .where(CheckGroup.owner_id == user_id)
        .group_by(CheckGroup.id)
    )


def _out(group: CheckGroup, members: int, counted: int, down: int) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        alert_emails=group.alert_emails,
        created_at=group.created_at,
        status=tiered_status(counted, down),
        check_count=members,
        down_count=down,
    )


async def _get(session: AsyncSession, user_id: int, group_id: int) -> GroupOut:
    stmt = _with_status(user_id).where(CheckGroup.id == group_id)
    row = (await session.execute(stmt.execution_options(populate_existing=True))).one_or_none()
    if row is None:
        raise _not_found()
    return _out(*row)


async def _owned(session: AsyncSession, user_id: int, group_id: int) -> CheckGroup:
    group = (
        await session.scalars(
            select(CheckGroup).where(CheckGroup.id == group_id, CheckGroup.owner_id == user_id)
        )
    ).one_or_none()
    if group is None:
        raise _not_found()
    return group


@router.get("")
async def list_groups(user_id: UserId, session: Session) -> list[GroupOut]:
    stmt = _with_status(user_id).order_by(CheckGroup.name, CheckGroup.id)
    return [_out(*row) for row in await session.execute(stmt)]


@router.post("", status_code=status.HTTP_201_CREATED, responses={409: {}})
async def create_group(body: GroupIn, user_id: UserId, session: Session) -> GroupOut:
    group = CheckGroup(owner_id=user_id, **body.model_dump())
    session.add(group)
    try:
        await session.commit()
    except IntegrityError:  # (owner_id, name) is unique
        raise _duplicate() from None
    return await _get(session, user_id, group.id)


@router.get("/{group_id}")
async def get_group(group_id: int, user_id: UserId, session: Session) -> GroupOut:
    return await _get(session, user_id, group_id)


@router.patch("/{group_id}", responses={409: {}})
async def update_group(
    group_id: int, body: GroupPatch, user_id: UserId, session: Session
) -> GroupOut:
    """A new alert list applies to emails queued from now on."""
    group = await _owned(session, user_id, group_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    try:
        await session.commit()
    except IntegrityError:
        raise _duplicate() from None
    return await _get(session, user_id, group_id)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: int, user_id: UserId, session: Session) -> None:
    deleted = (
        await session.execute(
            delete(CheckGroup)
            .where(CheckGroup.id == group_id, CheckGroup.owner_id == user_id)
            .returning(CheckGroup.id)
        )
    ).one_or_none()
    if deleted is None:
        raise _not_found()
    await session.commit()
