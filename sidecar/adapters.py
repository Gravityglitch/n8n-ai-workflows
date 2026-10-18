"""Outbound adapters. Every external system sits behind a Protocol with a DryRun implementation.

DryRun adapters record what *would* have been sent and return a fake id, so the approval
queue, tests and the default docker-compose stack never touch a real CRM, Slack or mailbox.
Live adapters are thin httpx calls and are only selected when *_MODE=live and a token is set.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

import httpx
import structlog

from sidecar.config import Settings

log = structlog.get_logger(__name__)


class CRM(Protocol):
    def create_contact(self, lead: dict[str, Any]) -> dict[str, Any]: ...


class Chat(Protocol):
    def post(self, channel: str, text: str) -> dict[str, Any]: ...


class Email(Protocol):
    def send(self, to: str, subject: str, body: str) -> dict[str, Any]: ...


class WorkflowCallback(Protocol):
    def resume(self, url: str, payload: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Dry-run implementations (default)
# --------------------------------------------------------------------------


@dataclass
class DryRunCRM:
    created: list[dict[str, Any]] = field(default_factory=list)

    def create_contact(self, lead: dict[str, Any]) -> dict[str, Any]:
        self.created.append(lead)
        contact_id = f"dryrun-contact-{len(self.created):04d}"
        log.info("crm.dry_run.create_contact", contact_id=contact_id, email=lead.get("email"))
        return {"contact_id": contact_id, "mode": "dry_run"}


@dataclass
class DryRunChat:
    posted: list[tuple[str, str]] = field(default_factory=list)

    def post(self, channel: str, text: str) -> dict[str, Any]:
        self.posted.append((channel, text))
        log.info("slack.dry_run.post", channel=channel, chars=len(text))
        return {"ts": f"dryrun-{len(self.posted)}", "channel": channel, "mode": "dry_run"}


@dataclass
class DryRunEmail:
    sent: list[dict[str, str]] = field(default_factory=list)

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        log.info("email.dry_run.send", to=to, subject=subject)
        return {"message_id": f"dryrun-msg-{len(self.sent):04d}", "mode": "dry_run"}


@dataclass
class DryRunCallback:
    resumed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def resume(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.resumed.append((url, payload))
        log.info("n8n.dry_run.resume", url=url)
        return {"resumed": True, "mode": "dry_run"}


# --------------------------------------------------------------------------
# Live implementations (thin, opt-in)
# --------------------------------------------------------------------------


class HubSpotCRM:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url="https://api.hubapi.com", timeout=15)
        self.headers = {"Authorization": f"Bearer {token}"}

    def create_contact(self, lead: dict[str, Any]) -> dict[str, Any]:
        first, _, last = str(lead.get("name", "")).partition(" ")
        properties = {
            "email": lead.get("email"),
            "firstname": first,
            "lastname": last,
            "company": lead.get("company"),
            "jobtitle": lead.get("role"),
        }
        resp = self.client.post(
            "/crm/v3/objects/contacts",
            json={"properties": {k: v for k, v in properties.items() if v}},
            headers=self.headers,
        )
        resp.raise_for_status()
        return {"contact_id": resp.json()["id"], "mode": "live"}


class SlackChat:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url="https://slack.com/api", timeout=15)
        self.headers = {"Authorization": f"Bearer {token}"}

    def post(self, channel: str, text: str) -> dict[str, Any]:
        resp = self.client.post(
            "/chat.postMessage", json={"channel": channel, "text": text}, headers=self.headers
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack error: {data.get('error')}")
        return {"ts": data["ts"], "channel": data["channel"], "mode": "live"}


class GmailEmail:
    def __init__(self, token: str, sender: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url="https://gmail.googleapis.com", timeout=15)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.sender = sender

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        msg = EmailMessage()
        msg["To"], msg["From"], msg["Subject"] = to, self.sender, subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = self.client.post(
            "/gmail/v1/users/me/messages/send", json={"raw": raw}, headers=self.headers
        )
        resp.raise_for_status()
        return {"message_id": resp.json()["id"], "mode": "live"}


class HttpCallback:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=15)

    def resume(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.post(url, json=payload)
        resp.raise_for_status()
        return {"resumed": True, "status_code": resp.status_code, "mode": "live"}


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


@dataclass
class Adapters:
    crm: CRM
    chat: Chat
    email: Email
    callback: WorkflowCallback

    @classmethod
    def dry_run(cls) -> Adapters:
        return cls(DryRunCRM(), DryRunChat(), DryRunEmail(), DryRunCallback())

    @classmethod
    def from_settings(cls, settings: Settings) -> Adapters:
        adapters = cls.dry_run()
        if settings.crm_mode == "live" and settings.hubspot_access_token:
            adapters.crm = HubSpotCRM(settings.hubspot_access_token)
        if settings.slack_mode == "live" and settings.slack_bot_token:
            adapters.chat = SlackChat(settings.slack_bot_token)
        if settings.email_mode == "live" and settings.gmail_access_token:
            adapters.email = GmailEmail(settings.gmail_access_token, settings.email_from)
        if settings.n8n_callback_mode == "live":
            adapters.callback = HttpCallback()
        return adapters
