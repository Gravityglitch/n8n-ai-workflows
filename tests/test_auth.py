from fastapi.testclient import TestClient

from sidecar.adapters import Adapters
from sidecar.api import create_app
from sidecar.config import Settings
from sidecar.llm import FixtureLLM


def test_health_is_public(client: TestClient) -> None:
    resp = client.get("/health", headers={"X-API-Key": ""})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "llm": "fixture", "auth_enabled": True}


def test_missing_or_wrong_key_is_401(client: TestClient) -> None:
    assert client.get("/pending", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/pending", headers={"X-API-Key": "wrong"}).status_code == 401


def test_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/pending", headers={"X-Request-ID": "req-42"})
    assert resp.headers["X-Request-ID"] == "req-42"
    assert client.get("/pending").headers["X-Request-ID"]


def test_auth_disabled_without_keys() -> None:
    settings = Settings(_env_file=None, app_env="test", api_keys=[], database_url="sqlite://")
    with TestClient(create_app(settings, llm=FixtureLLM(), adapters=Adapters.dry_run())) as client:
        assert client.get("/pending").status_code == 200
        assert client.get("/health").json()["auth_enabled"] is False
