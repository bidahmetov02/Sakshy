from fastapi.testclient import TestClient

from emulator.main import app


def test_ok_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/ok")
    assert response.status_code == 200
    assert response.text == "ok"
