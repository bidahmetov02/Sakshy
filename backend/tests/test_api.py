from fastapi.testclient import TestClient

from api.main import create_app
from core.settings import Settings


def test_health_reports_db_ok(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"db": "ok"}


def test_docs_served_under_api_prefix(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/openapi.json").status_code == 200
