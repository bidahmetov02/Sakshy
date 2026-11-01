"""Register, login, logout, the session cookie (`design/sequences.md` (e))."""

from datetime import UTC, datetime, timedelta

import httpx2
import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from api.security import ALGORITHM, COOKIE
from core.models import User
from core.settings import Settings
from tests.conftest import PASSWORD, SignIn
from tests.factories import rows


def creds(email: str = "alice@x.io", password: str = PASSWORD) -> dict[str, str]:
    return {"email": email, "password": password}


def token(settings: Settings, **claims: object) -> str:
    assert settings.jwt_secret is not None
    secret = settings.jwt_secret.get_secret_value()
    return jwt.encode(claims, secret, algorithm=ALGORITHM)  # pyright: ignore[reportUnknownMemberType]


async def test_register_creates_user_and_signs_in(
    engine: AsyncEngine, anon: httpx2.AsyncClient
) -> None:
    response = await anon.post("/api/auth/register", json=creds())

    assert response.status_code == 201
    user = response.json()
    assert user == {"id": user["id"], "email": "alice@x.io"}
    assert (await anon.get("/api/auth/me")).json() == user
    [stored] = await rows(engine, User)
    assert stored.password_hash.startswith("$argon2id$")


async def test_session_cookie_attributes(anon: httpx2.AsyncClient) -> None:
    response = await anon.post("/api/auth/register", json=creds())

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("httponly", "secure", "samesite=lax", "path=/", "max-age=86400"):
        assert attribute in cookie


async def test_register_rejects_duplicate_email_ignoring_case(anon: httpx2.AsyncClient) -> None:
    await anon.post("/api/auth/register", json=creds("alice@x.io"))
    response = await anon.post("/api/auth/register", json=creds("ALICE@x.io"))
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [creds(email="not-an-email"), creds(password="short"), creds(password="x" * 129)],
)
async def test_register_validates_input(anon: httpx2.AsyncClient, body: dict[str, str]) -> None:
    assert (await anon.post("/api/auth/register", json=body)).status_code == 422


async def test_login_with_any_email_case_sets_the_cookie(
    anon: httpx2.AsyncClient, sign_in: SignIn
) -> None:
    await sign_in("alice@x.io")
    response = await anon.post("/api/auth/login", json=creds("Alice@X.io"))

    assert response.status_code == 204
    assert COOKIE in anon.cookies
    assert (await anon.get("/api/auth/me")).json()["email"] == "alice@x.io"


@pytest.mark.parametrize("body", [creds(password="wrong password"), creds(email="nobody@x.io")])
async def test_login_failures_look_the_same(
    anon: httpx2.AsyncClient, sign_in: SignIn, body: dict[str, str]
) -> None:
    await sign_in("alice@x.io")
    response = await anon.post("/api/auth/login", json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid email or password"}
    assert COOKIE not in anon.cookies


async def test_logout_clears_the_cookie(sign_in: SignIn) -> None:
    alice = await sign_in("alice@x.io")
    assert (await alice.post("/api/auth/logout")).status_code == 204
    assert COOKIE not in alice.cookies
    assert (await alice.get("/api/auth/me")).status_code == 401


async def test_requests_without_a_cookie_are_401(anon: httpx2.AsyncClient) -> None:
    for path in ("/api/auth/me", "/api/checks"):
        assert (await anon.get(path)).status_code == 401
    assert (await anon.post("/api/auth/logout")).status_code == 401


async def test_rejects_expired_forged_and_malformed_tokens(
    anon: httpx2.AsyncClient, settings: Settings, sign_in: SignIn
) -> None:
    await sign_in("alice@x.io")
    hour = datetime.now(UTC) + timedelta(hours=1)
    bad = [
        token(settings, sub="1", exp=datetime.now(UTC) - timedelta(seconds=1)),  # expired
        token(settings, sub="1"),  # no expiry
        token(settings, sub="abc", exp=hour),
        jwt.encode({"sub": "1", "exp": hour}, "y" * 40, algorithm=ALGORITHM),  # pyright: ignore[reportUnknownMemberType]
        "not.a.jwt",
    ]
    for value in bad:
        response = await anon.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={value}"})
        assert response.status_code == 401, value

    valid = token(settings, sub="1", exp=hour)
    response = await anon.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={valid}"})
    assert response.status_code == 200
