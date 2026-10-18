"""Business logic behind /score, /triage and /summarise.

Each service asks the LLM for a structured object and falls back to the deterministic rule
engine if the model fails after retries. The `source` field on every result says which one ran.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel

from sidecar import rules
from sidecar.db import Database, IdempotencyRow
from sidecar.llm import (
    LLM,
    SCORE_SYSTEM,
    SUMMARISE_SYSTEM,
    TRIAGE_SYSTEM,
    LLMError,
)
from sidecar.models import (
    EmailTriage,
    Lead,
    LeadScore,
    PageSummary,
    ScoreResponse,
    SummarisedPage,
    SummariseRequest,
    SummariseResponse,
    TriagedEmail,
    TriageRequest,
    TriageResponse,
)

log = structlog.get_logger(__name__)

LABEL_BY_CATEGORY = {
    "customer": "Triage/Customer",
    "sales": "Triage/Sales",
    "billing": "Triage/Billing",
    "internal": "Triage/Internal",
    "newsletter": "Triage/Newsletter",
    "spam": "Triage/Spam",
}


class IdempotencyConflict(ValueError):
    """Same Idempotency-Key reused with a different request body."""


def _structured_or_fallback[T: BaseModel](
    llm: LLM, step: str, system: str, payload: BaseModel, schema: type[T], fallback: T
) -> tuple[T, str]:
    try:
        return llm.structured(step, system, payload.model_dump_json(), schema), "llm"
    except LLMError as exc:
        log.warning("llm.fallback", step=step, error=str(exc))
        return fallback, "rules"


class Services:
    def __init__(self, llm: LLM, db: Database, score_threshold: int) -> None:
        self.llm = llm
        self.db = db
        self.score_threshold = score_threshold

    # --- /score -------------------------------------------------------------

    def score(self, lead: Lead, idempotency_key: str | None = None) -> tuple[ScoreResponse, bool]:
        """Returns (response, replayed). Replays return the stored response unchanged."""
        request_hash = hashlib.sha256(lead.model_dump_json().encode()).hexdigest()
        if idempotency_key:
            with self.db.session() as s:
                existing = s.get(IdempotencyRow, idempotency_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(idempotency_key)
                return ScoreResponse.model_validate(existing.response), True

        scored, source = _structured_or_fallback(
            self.llm, "score", SCORE_SYSTEM, lead, LeadScore, rules.score_lead(lead)
        )
        response = ScoreResponse(
            lead_id=uuid.uuid4().hex,
            score=scored.score,
            tier=scored.tier,
            qualified=scored.score >= self.score_threshold,
            threshold=self.score_threshold,
            reasons=scored.reasons,
            suggested_next_step=scored.suggested_next_step,
            source=source,
        )
        if idempotency_key:
            with self.db.session() as s:
                s.add(
                    IdempotencyRow(
                        key=idempotency_key,
                        request_hash=request_hash,
                        response=json.loads(response.model_dump_json()),
                    )
                )
        log.info("lead.scored", lead_id=response.lead_id, score=response.score, source=source)
        return response, False

    # --- /triage ------------------------------------------------------------

    def triage(self, request: TriageRequest) -> TriageResponse:
        results: list[TriagedEmail] = []
        for email in request.emails:
            triaged, source = _structured_or_fallback(
                self.llm, "triage", TRIAGE_SYSTEM, email, EmailTriage, rules.triage_email(email)
            )
            results.append(
                TriagedEmail(
                    **triaged.model_dump(),
                    id=email.id,
                    sender=email.sender,
                    subject=email.subject,
                    label=LABEL_BY_CATEGORY[triaged.category.value],
                    source=source,
                )
            )
        return TriageResponse(results=results, digest_markdown=render_digest(results))

    # --- /summarise ---------------------------------------------------------

    def summarise(self, request: SummariseRequest) -> SummariseResponse:
        pages: list[SummarisedPage] = []
        for page in request.pages:
            summary, source = _structured_or_fallback(
                self.llm,
                "summarise",
                SUMMARISE_SYSTEM,
                page,
                PageSummary,
                rules.summarise_page(page),
            )
            pages.append(SummarisedPage(**summary.model_dump(), url=page.url, source=source))
        generated_at = datetime.now(UTC)
        return SummariseResponse(
            report_title=request.report_title,
            generated_at=generated_at,
            pages=pages,
            markdown=render_report(request.report_title, generated_at, pages),
        )


def _triage_sort_key(email: TriagedEmail) -> tuple[int, str]:
    return ({"high": 0, "medium": 1, "low": 2}[email.priority.value], email.subject)


def render_digest(results: list[TriagedEmail]) -> str:
    if not results:
        return "*Inbox digest*: nothing new."
    lines = [f"*Inbox digest* - {len(results)} unread"]
    for email in sorted(results, key=_triage_sort_key):
        reply = " (reply needed)" if email.needs_reply else ""
        lines.append(
            f"- [{email.priority.value.upper()}] *{email.subject or '(no subject)'}* "
            f"from {email.sender} - {email.category.value}{reply}"
        )
        lines.append(f"  {email.summary}")
        lines.extend(f"  - [ ] {item}" for item in email.action_items)
    return "\n".join(lines)


def render_report(title: str, generated_at: datetime, pages: list[SummarisedPage]) -> str:
    lines = [f"# {title}", "", f"_Generated {generated_at:%Y-%m-%d %H:%M} UTC_", ""]
    for page in pages:
        lines += [f"## {page.title}", "", f"<{page.url}>", "", page.summary, ""]
        lines += [f"- {point}" for point in page.key_points]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
