from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from emulator.main import app, sites


@pytest.fixture
def client() -> Generator[TestClient]:
    sites.clear()
    with TestClient(app) as client:
        yield client
    sites.clear()


def test_ok_endpoint(client: TestClient) -> None:
    response = client.get("/ok")
    assert response.status_code == 200
    assert response.text == "ok"


def test_status_endpoint(client: TestClient) -> None:
    assert client.get("/status/503").status_code == 503


def test_body_endpoint(client: TestClient) -> None:
    assert client.get("/body", params={"text": "hello"}).text == "hello"


def test_big_endpoint(client: TestClient) -> None:
    assert len(client.get("/big", params={"kib": 3}).content) == 3 * 1024


def test_redirect_chain_ends_at_ok(client: TestClient) -> None:
    response = client.get("/redirect/3", follow_redirects=True)
    assert response.text == "ok"
    assert len(response.history) == 3


def test_unknown_site_is_healthy(client: TestClient) -> None:
    response = client.get("/sites/shop")
    assert response.status_code == 200
    assert response.text == "shop: ok"


def test_site_can_be_flipped_to_error_and_back(client: TestClient) -> None:
    client.put("/control/sites/shop", json={"mode": "error", "status": 502})
    assert client.get("/sites/shop").status_code == 502
    assert client.get("/sites/blog").status_code == 200

    client.delete("/control/sites/shop")
    assert client.get("/sites/shop").status_code == 200


def test_slow_site_waits(client: TestClient) -> None:
    client.put("/control/sites/shop", json={"mode": "slow", "delay_ms": 200})
    response = client.get("/sites/shop")
    assert response.status_code == 200
    assert response.elapsed.total_seconds() >= 0.2


def test_control_lists_configured_sites(client: TestClient) -> None:
    client.put("/control/sites/shop", json={"mode": "hang"})
    assert client.get("/control/sites").json() == {
        "shop": {"mode": "hang", "delay_ms": 3000, "status": 500}
    }


def test_control_rejects_unknown_mode(client: TestClient) -> None:
    assert client.put("/control/sites/shop", json={"mode": "on-fire"}).status_code == 422
