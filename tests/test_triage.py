from fastapi.testclient import TestClient

from sidecar.adapters import Adapters
from sidecar.api import create_app
from sidecar.config import Settings
from sidecar.llm import FixtureLLM
from sidecar.services import render_digest
from tests.conftest import TEST_API_KEY


def test_triage_classifies_and_labels(client: TestClient, sample_emails: list[dict]) -> None:
    resp = client.post("/triage", json={"emails": sample_emails})
    assert resp.status_code == 200, resp.text
    by_id = {r["id"]: r for r in resp.json()["results"]}
    assert by_id["msg-1001"]["category"] == "customer"
    assert by_id["msg-1001"]["priority"] == "high"
    assert by_id["msg-1001"]["needs_reply"] is True
    assert by_id["msg-1001"]["label"] == "Triage/Customer"
    assert any("look at this today" in a for a in by_id["msg-1001"]["action_items"])
    assert by_id["msg-1002"]["category"] == "sales"
    assert by_id["msg-1003"]["category"] == "billing"
    assert by_id["msg-1004"]["category"] == "newsletter"
    assert by_id["msg-1004"]["priority"] == "low"
    assert by_id["msg-1005"]["category"] == "spam"
    assert by_id["msg-1005"]["needs_reply"] is False
    assert all(r["source"] == "llm" for r in by_id.values())


def test_digest_orders_high_priority_first(client: TestClient, sample_emails: list[dict]) -> None:
    digest = client.post("/triage", json={"emails": sample_emails}).json()["digest_markdown"]
    lines = [line for line in digest.splitlines() if line.startswith("- [")]
    assert lines[0].startswith("- [HIGH]")
    assert lines[-1].startswith("- [LOW]")
    assert "5 unread" in digest
    assert "- [ ] " in digest  # action items rendered as checkboxes


def test_empty_inbox(client: TestClient) -> None:
    body = client.post("/triage", json={"emails": []}).json()
    assert body["results"] == []
    assert "nothing new" in body["digest_markdown"]
    assert render_digest([]) == body["digest_markdown"]


def test_triage_fallback_marks_source_rules(settings: Settings, sample_emails: list[dict]) -> None:
    app = create_app(settings, llm=FixtureLLM(fail_steps={"triage"}), adapters=Adapters.dry_run())
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        results = client.post("/triage", json={"emails": sample_emails[:2]}).json()["results"]
    assert {r["source"] for r in results} == {"rules"}


def test_email_accepts_from_alias_or_sender(client: TestClient) -> None:
    emails = [
        {"id": "a", "from": "x@example.test", "subject": "Invoice", "body": "payment due"},
        {"id": "b", "sender": "y@example.test", "subject": "Hello", "body": "team lunch"},
    ]
    results = client.post("/triage", json={"emails": emails}).json()["results"]
    assert [r["sender"] for r in results] == ["x@example.test", "y@example.test"]
    assert results[1]["category"] == "internal"
