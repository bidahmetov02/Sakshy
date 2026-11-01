"""Register, log in and out, who am I (`design/sequences.md` (e))."""

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from api.deps import AppSettings, Session, UserId
from api.schemas import Credentials, UserOut
from api.security import (
    clear_session_cookie,
    hash_password,
    set_session_cookie,
    verify_password,
)
from core.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: Credentials, response: Response, session: Session, settings: AppSettings
) -> UserOut:
    user = User(email=body.email, password_hash=await hash_password(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # users_email_uq, case-insensitive
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from None
    set_session_cookie(response, settings, user.id)
    return UserOut(id=user.id, email=user.email)


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(
    body: Credentials, response: Response, session: Session, settings: AppSettings
) -> None:
    row = (
        await session.execute(
            select(User.id, User.password_hash).where(
                func.lower(User.email) == func.lower(body.email)
            )
        )
    ).one_or_none()
    valid = await verify_password(body.password, None if row is None else row.password_hash)
    if row is None or not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    set_session_cookie(response, settings, row.id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: UserId, response: Response, settings: AppSettings) -> None:
    """Drops the cookie. The token itself stays valid until it expires."""
    clear_session_cookie(response, settings)


@router.get("/me")
async def me(user_id: UserId, session: Session) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return UserOut(id=user.id, email=user.email)
