"""Pydantic schemas shared by the API, the LLM adapter and the n8n workflows."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# Deliberately permissive: fictional sample data uses reserved TLDs such as .test, which the
# stricter email-validator package rejects.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --------------------------------------------------------------------------
# /score
# --------------------------------------------------------------------------


class Lead(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320)
    company: str | None = None
    role: str | None = None
    company_size: int | None = Field(default=None, ge=0)
    budget_usd: float | None = Field(default=None, ge=0)
    message: str = Field(default="", max_length=5000)
    source: str = Field(default="web_form", max_length=100)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class LeadScore(BaseModel):
    """Structured output the model must produce. Also what the rule engine produces."""

    score: int = Field(ge=0, le=100, description="0-100 likelihood of becoming a customer")
    tier: Tier
    reasons: list[str] = Field(min_length=1, max_length=6)
    suggested_next_step: str = Field(max_length=300)


class ScoreResponse(BaseModel):
    lead_id: str
    score: int
    tier: Tier
    qualified: bool
    threshold: int
    reasons: list[str]
    suggested_next_step: str
    source: str = Field(description="'llm' or 'rules' (deterministic fallback)")


# --------------------------------------------------------------------------
# /triage
# --------------------------------------------------------------------------


class EmailIn(BaseModel):
    id: str
    sender: str = Field(alias="from")
    subject: str = ""
    body: str = Field(default="", max_length=20000)
    received_at: datetime | None = None

    model_config = {"populate_by_name": True}


class EmailCategory(StrEnum):
    CUSTOMER = "customer"
    SALES = "sales"
    BILLING = "billing"
    INTERNAL = "internal"
    NEWSLETTER = "newsletter"
    SPAM = "spam"


class Priority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EmailTriage(BaseModel):
    """Structured output per email."""

    category: EmailCategory
    priority: Priority
    summary: str = Field(max_length=400)
    action_items: list[str] = Field(default_factory=list, max_length=8)
    needs_reply: bool


class TriagedEmail(EmailTriage):
    id: str
    sender: str
    subject: str
    label: str = Field(description="Gmail label the workflow should apply")
    source: str


class TriageRequest(BaseModel):
    emails: list[EmailIn] = Field(max_length=100)


class TriageResponse(BaseModel):
    results: list[TriagedEmail]
    digest_markdown: str


# --------------------------------------------------------------------------
# /summarise
# --------------------------------------------------------------------------


class PageIn(BaseModel):
    url: str
    title: str | None = None
    text: str = Field(default="", max_length=200000)


class PageSummary(BaseModel):
    """Structured output per page."""

    title: str = Field(max_length=200)
    summary: str = Field(max_length=800)
    key_points: list[str] = Field(default_factory=list, max_length=8)


class SummarisedPage(PageSummary):
    url: str
    source: str


class SummariseRequest(BaseModel):
    report_title: str = Field(default="Weekly research digest", max_length=200)
    pages: list[PageIn] = Field(min_length=1, max_length=50)


class SummariseResponse(BaseModel):
    report_title: str
    generated_at: datetime
    pages: list[SummarisedPage]
    markdown: str


# --------------------------------------------------------------------------
# Approval queue
# --------------------------------------------------------------------------


class ActionType(StrEnum):
    CREATE_CRM_CONTACT = "create_crm_contact"
    SEND_EMAIL = "send_email"
    POST_SLACK = "post_slack"


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class ActionCreate(BaseModel):
    type: ActionType
    payload: dict = Field(default_factory=dict)
    resume_url: str | None = Field(
        default=None,
        description="n8n Wait-node resume URL ($execution.resumeUrl). Called after execution.",
    )
    requested_by: str = "n8n"


class ActionOut(BaseModel):
    id: str
    type: ActionType
    status: ActionStatus
    payload: dict
    result: dict | None
    error: str | None
    resume_url: str | None
    requested_by: str
    created_at: datetime
    decided_at: datetime | None


class Decision(BaseModel):
    decided_by: str = "human"
    note: str | None = None
