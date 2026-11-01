"""Request dependencies: settings, a database session, the signed-in user."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import COOKIE, user_id_from_token
from core.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncGenerator[AsyncSession]:
    """One session per request. Endpoints commit explicitly; anything uncommitted is
    rolled back when the session closes."""
    async with AsyncSession(request.app.state.engine, expire_on_commit=False) as session:
        yield session


AppSettings = Annotated[Settings, Depends(get_settings)]
Session = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False: a missing cookie gets the same 401 as a bad one.
_cookie = APIKeyCookie(name=COOKIE, auto_error=False)


def current_user_id(settings: AppSettings, token: Annotated[str | None, Depends(_cookie)]) -> int:
    user_id = None if token is None else user_id_from_token(settings, token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return user_id


UserId = Annotated[int, Depends(current_user_id)]
