"""Approval queue: the only code path that performs writes to external systems.

n8n registers an action (POST /actions) with its Wait-node resume URL, a human approves or
rejects it (POST /approve|reject/{id}), and on approval the write is executed through an
adapter and the n8n execution is resumed with the result.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from sidecar.adapters import Adapters
from sidecar.db import ActionRow, Database
from sidecar.models import ActionCreate, ActionOut, ActionStatus, ActionType

log = structlog.get_logger(__name__)

# Deterministic, fictional booking slots offered to qualified leads.
BOOKING_SLOTS = ["2026-11-03T10:00:00Z", "2026-11-03T15:00:00Z", "2026-11-04T11:00:00Z"]


class ActionNotFound(LookupError):
    pass


class ActionNotPending(ValueError):
    pass


class ApprovalQueue:
    def __init__(self, db: Database, adapters: Adapters) -> None:
        self.db = db
        self.adapters = adapters

    def create(self, request: ActionCreate) -> ActionOut:
        row = ActionRow(
            id=uuid.uuid4().hex,
            type=request.type.value,
            payload=request.payload,
            resume_url=request.resume_url,
            requested_by=request.requested_by,
        )
        with self.db.session() as s:
            s.add(row)
        log.info("action.created", action_id=row.id, type=row.type)
        return _to_out(row)

    def get(self, action_id: str) -> ActionOut:
        with self.db.session() as s:
            row = s.get(ActionRow, action_id)
        if row is None:
            raise ActionNotFound(action_id)
        return _to_out(row)

    def pending(self) -> list[ActionOut]:
        with self.db.session() as s:
            rows = (
                s.query(ActionRow)
                .filter(ActionRow.status == ActionStatus.PENDING.value)
                .order_by(ActionRow.created_at)
                .all()
            )
        return [_to_out(r) for r in rows]

    def approve(self, action_id: str, decided_by: str, note: str | None = None) -> ActionOut:
        with self.db.session() as s:
            row = s.get(ActionRow, action_id)
            if row is None:
                raise ActionNotFound(action_id)
            if row.status != ActionStatus.PENDING.value:
                raise ActionNotPending(f"{action_id} is {row.status}")
            row.decided_by, row.note, row.decided_at = decided_by, note, datetime.now(UTC)
            try:
                row.result = self._perform(ActionType(row.type), row.payload)
                row.status = ActionStatus.APPROVED.value
            except Exception as exc:  # noqa: BLE001 - recorded on the row, never swallowed silently
                row.status = ActionStatus.FAILED.value
                row.error = f"{type(exc).__name__}: {exc}"
                log.error("action.failed", action_id=row.id, error=row.error)
            s.add(row)

        out = _to_out(row)
        if row.resume_url:
            self._resume(out)
        log.info("action.decided", action_id=row.id, status=row.status, decided_by=decided_by)
        return out

    def reject(self, action_id: str, decided_by: str, note: str | None = None) -> ActionOut:
        with self.db.session() as s:
            row = s.get(ActionRow, action_id)
            if row is None:
                raise ActionNotFound(action_id)
            if row.status != ActionStatus.PENDING.value:
                raise ActionNotPending(f"{action_id} is {row.status}")
            row.status = ActionStatus.REJECTED.value
            row.decided_by, row.note, row.decided_at = decided_by, note, datetime.now(UTC)
            s.add(row)
        out = _to_out(row)
        if row.resume_url:
            self._resume(out)
        log.info("action.decided", action_id=row.id, status=row.status, decided_by=decided_by)
        return out

    def _perform(self, action_type: ActionType, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type is ActionType.CREATE_CRM_CONTACT:
            result = self.adapters.crm.create_contact(payload["lead"])
            return {**result, "proposed_slots": BOOKING_SLOTS}
        if action_type is ActionType.SEND_EMAIL:
            return self.adapters.email.send(payload["to"], payload["subject"], payload["body"])
        if action_type is ActionType.POST_SLACK:
            return self.adapters.chat.post(payload["channel"], payload["text"])
        raise ValueError(f"unsupported action type {action_type}")  # pragma: no cover

    def _resume(self, action: ActionOut) -> None:
        assert action.resume_url is not None
        body = action.model_dump(mode="json")
        try:
            self.adapters.callback.resume(action.resume_url, body)
        except Exception as exc:  # noqa: BLE001 - n8n may have timed out; the decision is already saved
            log.warning("action.resume_failed", action_id=action.id, error=str(exc))


def _utc(value: datetime | None) -> datetime | None:
    """SQLite stores naive datetimes; present them as UTC consistently."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _to_out(row: ActionRow) -> ActionOut:
    return ActionOut(
        id=row.id,
        type=ActionType(row.type),
        status=ActionStatus(row.status),
        payload=row.payload,
        result=row.result,
        error=row.error,
        resume_url=row.resume_url,
        requested_by=row.requested_by,
        created_at=_utc(row.created_at),
        decided_at=_utc(row.decided_at),
    )
