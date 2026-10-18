from fastapi.testclient import TestClient

from sidecar.adapters import Adapters
from sidecar.api import create_app
from sidecar.config import Settings
from sidecar.llm import FixtureLLM
from tests.conftest import TEST_API_KEY


def test_hot_lead_is_qualified(client: TestClient, sample_leads: list[dict]) -> None:
    resp = client.post("/score", json=sample_leads[0])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["qualified"] is True
    assert body["tier"] == "hot"
    assert body["score"] >= body["threshold"] == 70
    assert "business email domain" in body["reasons"]
    assert body["source"] == "llm"
    assert resp.headers["Idempotent-Replay"] == "false"


def test_cold_lead_is_not_qualified(client: TestClient, sample_leads: list[dict]) -> None:
    body = client.post("/score", json=sample_leads[1]).json()
    assert body["qualified"] is False
    assert body["tier"] == "cold"
    assert "very short message" in body["reasons"]


def test_warm_lead(client: TestClient, sample_leads: list[dict]) -> None:
    body = client.post("/score", json=sample_leads[2]).json()
    assert body["tier"] == "warm"
    assert body["qualified"] is False


def test_idempotency_replay_returns_same_response(
    client: TestClient, sample_leads: list[dict]
) -> None:
    headers = {"Idempotency-Key": "lead-1"}
    first = client.post("/score", json=sample_leads[0], headers=headers)
    second = client.post("/score", json=sample_leads[0], headers=headers)
    assert first.json() == second.json()
    assert first.json()["lead_id"] == second.json()["lead_id"]
    assert second.headers["Idempotent-Replay"] == "true"


def test_idempotency_key_reuse_with_different_body_conflicts(
    client: TestClient, sample_leads: list[dict]
) -> None:
    headers = {"Idempotency-Key": "lead-2"}
    assert client.post("/score", json=sample_leads[0], headers=headers).status_code == 200
    resp = client.post("/score", json=sample_leads[1], headers=headers)
    assert resp.status_code == 409


def test_llm_failure_falls_back_to_rules(settings: Settings, sample_leads: list[dict]) -> None:
    llm = FixtureLLM(fail_steps={"score"})
    app = create_app(settings, llm=llm, adapters=Adapters.dry_run())
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        body = client.post("/score", json=sample_leads[0]).json()
    assert body["source"] == "rules"
    assert body["qualified"] is True
    assert llm.calls == ["score"]


def test_invalid_lead_is_rejected(client: TestClient) -> None:
    resp = client.post("/score", json={"name": "", "email": "not-an-email"})
    assert resp.status_code == 422


def test_threshold_is_configurable(sample_leads: list[dict]) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_keys=[TEST_API_KEY],
        database_url="sqlite://",
        score_threshold=50,
    )
    app = create_app(settings, llm=FixtureLLM(), adapters=Adapters.dry_run())
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        body = client.post("/score", json=sample_leads[2]).json()
    assert body["threshold"] == 50
    assert body["tier"] == "warm"
    assert body["qualified"] is True  # warm lead clears the lowered bar
