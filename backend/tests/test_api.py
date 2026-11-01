import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from api.main import create_app
from core.settings import Settings
from worker import heartbeat


async def test_health_reports_db_ok_and_no_worker(anon: httpx2.AsyncClient) -> None:
    response = await anon.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"db": "ok", "worker_last_seen_s": None}


async def test_health_reports_seconds_since_last_worker_heartbeat(
    engine: AsyncEngine, anon: httpx2.AsyncClient
) -> None:
    await heartbeat.mark_alive(engine, "w1")
    assert (await anon.get("/api/health")).json() == {"db": "ok", "worker_last_seen_s": 0}


async def test_docs_served_under_api_prefix(anon: httpx2.AsyncClient) -> None:
    assert (await anon.get("/api/openapi.json")).status_code == 200


@pytest.mark.parametrize("secret", [None, "too-short"])
def test_refuses_to_start_without_a_usable_jwt_secret(
    settings: Settings, secret: str | None
) -> None:
    bad = settings.model_copy(update={"jwt_secret": secret and SecretStr(secret)})
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app(bad)
