"""Passwords (argon2) and the session cookie: a signed JWT in `access_token`
(`design/sequences.md` (e))."""

import asyncio
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from fastapi import Response
from pwdlib import PasswordHash

from core.settings import Settings

COOKIE = "access_token"
ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32

_hasher = PasswordHash.recommended()


async def hash_password(password: str) -> str:
    # argon2 takes tens of milliseconds of CPU: keep it off the event loop.
    return await asyncio.to_thread(_hasher.hash, password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("not a real password")


async def verify_password(password: str, password_hash: str | None) -> bool:
    """`None` (unknown email) still costs one verify, so timing doesn't reveal which emails
    have accounts."""
    if password_hash is None:
        await asyncio.to_thread(_hasher.verify, password, _dummy_hash())
        return False
    return await asyncio.to_thread(_hasher.verify, password, password_hash)


def _secret(settings: Settings) -> str:
    if settings.jwt_secret is None:
        raise RuntimeError("JWT_SECRET is not set")
    return settings.jwt_secret.get_secret_value()


def check_secret(settings: Settings) -> None:
    """Called at startup: refuse to run with no secret or a short one."""
    if len(_secret(settings)) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters")


def create_token(settings: Settings, user_id: int) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_s),
    }
    return jwt.encode(claims, _secret(settings), algorithm=ALGORITHM)  # pyright: ignore[reportUnknownMemberType]


def user_id_from_token(settings: Settings, token: str) -> int | None:
    """The user id, or None if the token is invalid, expired or malformed."""
    try:
        claims = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token, _secret(settings), algorithms=[ALGORITHM], options={"require": ["exp", "sub"]}
        )
        return int(claims["sub"])
    except jwt.InvalidTokenError, ValueError:
        return None


def set_session_cookie(response: Response, settings: Settings, user_id: int) -> None:
    response.set_cookie(
        COOKIE,
        create_token(settings, user_id),
        max_age=settings.access_token_ttl_s,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
