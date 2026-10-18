"""Live adapters against httpx.MockTransport; settings-driven wiring."""

import base64
import json

import httpx
import pytest

from sidecar.adapters import (
    Adapters,
    DryRunChat,
    GmailEmail,
    HttpCallback,
    HubSpotCRM,
    SlackChat,
)
from sidecar.config import Settings


def _client(handler, base_url: str) -> httpx.Client:  # noqa: ANN001
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def test_hubspot_create_contact() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "501"})

    crm = HubSpotCRM("tok", client=_client(handler, "https://api.hubapi.com"))
    result = crm.create_contact(
        {"name": "Priya Natarajan", "email": "p@nw.test", "company": "NW", "role": None}
    )
    assert result == {"contact_id": "501", "mode": "live"}
    assert seen["path"] == "/crm/v3/objects/contacts"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["properties"] == {
        "email": "p@nw.test",
        "firstname": "Priya",
        "lastname": "Natarajan",
        "company": "NW",
    }


def test_slack_post_and_error() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "ts": "1.2", "channel": "C1"})

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    chat = SlackChat("xoxb", client=_client(ok, "https://slack.com/api"))
    assert chat.post("#x", "hi")["ts"] == "1.2"
    with pytest.raises(RuntimeError, match="channel_not_found"):
        SlackChat("xoxb", client=_client(bad, "https://slack.com/api")).post("#x", "hi")


def test_gmail_send_encodes_rfc822() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = json.loads(request.content)["raw"]
        return httpx.Response(200, json={"id": "m-1"})

    mail = GmailEmail(
        "tok", "ops@example.test", client=_client(handler, "https://gmail.googleapis.com")
    )
    assert mail.send("lead@example.test", "Hello", "Body text")["message_id"] == "m-1"
    decoded = base64.urlsafe_b64decode(seen["raw"]).decode()
    assert "To: lead@example.test" in decoded
    assert "Subject: Hello" in decoded
    assert "Body text" in decoded


def test_http_callback_posts_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    cb = HttpCallback(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cb.resume("http://n8n:5678/webhook-waiting/1", {"status": "approved"})["resumed"]
    assert seen == {"url": "http://n8n:5678/webhook-waiting/1", "body": {"status": "approved"}}


def test_http_errors_propagate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        HubSpotCRM("tok", client=_client(handler, "https://api.hubapi.com")).create_contact({})


def test_from_settings_defaults_to_dry_run() -> None:
    adapters = Adapters.from_settings(Settings(_env_file=None))
    assert isinstance(adapters.chat, DryRunChat)
    assert adapters.crm.__class__.__name__ == "DryRunCRM"


def test_from_settings_wires_live_adapters() -> None:
    settings = Settings(
        _env_file=None,
        crm_mode="live",
        hubspot_access_token="h",
        slack_mode="live",
        slack_bot_token="s",
        email_mode="live",
        gmail_access_token="g",
        n8n_callback_mode="live",
    )
    adapters = Adapters.from_settings(settings)
    assert isinstance(adapters.crm, HubSpotCRM)
    assert isinstance(adapters.chat, SlackChat)
    assert isinstance(adapters.email, GmailEmail)
    assert isinstance(adapters.callback, HttpCallback)
