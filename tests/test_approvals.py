from fastapi.testclient import TestClient

from sidecar.adapters import Adapters, DryRunCallback, DryRunCRM, DryRunEmail
from sidecar.approvals import BOOKING_SLOTS

RESUME_URL = "http://n8n:5678/webhook-waiting/abc123"


def _create(client: TestClient, action_type: str = "create_crm_contact", **payload: object) -> dict:
    body = {
        "type": action_type,
        "payload": payload or {"lead": {"name": "Priya Natarajan", "email": "p@nw.test"}},
        "resume_url": RESUME_URL,
        "requested_by": "test",
    }
    resp = client.post("/actions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_list_pending(client: TestClient) -> None:
    created = _create(client)
    assert created["status"] == "pending"
    pending = client.get("/pending").json()
    assert [p["id"] for p in pending] == [created["id"]]
    assert client.get(f"/actions/{created['id']}").json()["type"] == "create_crm_contact"


def test_approve_creates_contact_and_resumes_n8n(client: TestClient, adapters: Adapters) -> None:
    created = _create(client)
    resp = client.post(f"/approve/{created['id']}", json={"decided_by": "vikrant", "note": "ok"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["result"]["contact_id"].startswith("dryrun-contact-")
    assert body["result"]["proposed_slots"] == BOOKING_SLOTS
    assert body["created_at"].endswith("Z") and body["decided_at"].endswith("Z")

    crm = adapters.crm
    assert isinstance(crm, DryRunCRM) and crm.created[0]["email"] == "p@nw.test"
    callback = adapters.callback
    assert isinstance(callback, DryRunCallback)
    url, payload = callback.resumed[0]
    assert url == RESUME_URL
    assert payload["status"] == "approved" and payload["id"] == created["id"]
    assert client.get("/pending").json() == []


def test_reject_does_not_write(client: TestClient, adapters: Adapters) -> None:
    created = _create(client)
    body = client.post(f"/reject/{created['id']}").json()
    assert body["status"] == "rejected"
    assert body["result"] is None
    assert isinstance(adapters.crm, DryRunCRM) and adapters.crm.created == []
    assert isinstance(adapters.callback, DryRunCallback) and len(adapters.callback.resumed) == 1


def test_send_email_and_slack_actions(client: TestClient, adapters: Adapters) -> None:
    email = _create(client, "send_email", to="lead@example.test", subject="Hi", body="Thanks")
    slack = _create(client, "post_slack", channel="#leads", text="hello")
    assert client.post(f"/approve/{email['id']}").json()["result"]["message_id"]
    assert client.post(f"/approve/{slack['id']}").json()["result"]["ts"]
    assert (
        isinstance(adapters.email, DryRunEmail)
        and adapters.email.sent[0]["to"] == "lead@example.test"
    )


def test_decisions_are_final(client: TestClient) -> None:
    created = _create(client)
    assert client.post(f"/approve/{created['id']}").status_code == 200
    assert client.post(f"/approve/{created['id']}").status_code == 409
    assert client.post(f"/reject/{created['id']}").status_code == 409


def test_unknown_action_is_404(client: TestClient) -> None:
    assert client.post("/approve/nope").status_code == 404
    assert client.post("/reject/nope").status_code == 404
    assert client.get("/actions/nope").status_code == 404


class _BrokenCRM:
    def create_contact(self, lead: dict) -> dict:
        raise ConnectionError("hubspot down")


class _BrokenCallback:
    def resume(self, url: str, payload: dict) -> dict:
        raise TimeoutError("n8n unreachable")


def test_adapter_failure_marks_action_failed(client: TestClient, adapters: Adapters) -> None:
    adapters.crm = _BrokenCRM()
    adapters.callback = _BrokenCallback()
    created = _create(client)
    body = client.post(f"/approve/{created['id']}").json()
    assert body["status"] == "failed"
    assert "hubspot down" in body["error"]
    # the resume failure is logged, not raised; the decision is persisted
    assert client.get(f"/actions/{created['id']}").json()["status"] == "failed"


def test_action_without_resume_url_skips_callback(client: TestClient, adapters: Adapters) -> None:
    resp = client.post(
        "/actions", json={"type": "post_slack", "payload": {"channel": "#x", "text": "y"}}
    )
    client.post(f"/approve/{resp.json()['id']}")
    assert isinstance(adapters.callback, DryRunCallback) and adapters.callback.resumed == []


def test_invalid_action_type_rejected(client: TestClient) -> None:
    assert client.post("/actions", json={"type": "delete_everything"}).status_code == 422
