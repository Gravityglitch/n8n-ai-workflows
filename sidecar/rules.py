"""Deterministic fallbacks. Used when the LLM is unavailable and by the fixture LLM backend.

Everything here is plain code so behaviour is reproducible in tests and offline evals.
"""

from __future__ import annotations

import re

from sidecar.models import (
    EmailCategory,
    EmailIn,
    EmailTriage,
    Lead,
    LeadScore,
    PageIn,
    PageSummary,
    Priority,
    Tier,
)

_FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "example.com"}
_BUYING_SIGNALS = ("pricing", "quote", "demo", "timeline", "budget", "implement", "integrate")
_SENIOR_TITLES = ("cto", "ceo", "founder", "vp", "head", "director")


def score_lead(lead: Lead) -> LeadScore:
    score = 20
    reasons: list[str] = []

    domain = lead.email.split("@")[-1].lower()
    if domain not in _FREE_MAIL:
        score += 15
        reasons.append("business email domain")

    if lead.company_size is not None:
        if lead.company_size >= 200:
            score += 20
            reasons.append("company size 200+")
        elif lead.company_size >= 20:
            score += 10
            reasons.append("company size 20+")

    if lead.budget_usd is not None:
        if lead.budget_usd >= 10_000:
            score += 25
            reasons.append("budget >= $10k")
        elif lead.budget_usd >= 2_000:
            score += 10
            reasons.append("budget >= $2k")

    text = lead.message.lower()
    hits = [w for w in _BUYING_SIGNALS if w in text]
    if hits:
        score += min(20, 7 * len(hits))
        reasons.append("buying signals: " + ", ".join(hits))

    role = (lead.role or "").lower()
    if any(t in role for t in _SENIOR_TITLES):
        score += 10
        reasons.append("senior decision maker")

    if len(lead.message.strip()) < 30:
        score -= 10
        reasons.append("very short message")

    score = max(0, min(100, score))
    tier = Tier.HOT if score >= 70 else Tier.WARM if score >= 40 else Tier.COLD
    next_step = {
        Tier.HOT: "Create CRM contact and offer discovery-call slots within 24h.",
        Tier.WARM: "Send tailored case study and follow up in 3 days.",
        Tier.COLD: "Add to nurture sequence; no manual outreach.",
    }[tier]
    return LeadScore(
        score=score, tier=tier, reasons=reasons or ["no signals"], suggested_next_step=next_step
    )


_TRIAGE_RULES: list[tuple[EmailCategory, tuple[str, ...]]] = [
    (EmailCategory.SPAM, ("you have won", "crypto giveaway", "act now", "unsubscribe to stop")),
    (EmailCategory.NEWSLETTER, ("newsletter", "weekly roundup", "unsubscribe")),
    (EmailCategory.BILLING, ("invoice", "receipt", "payment", "refund", "overdue")),
    (EmailCategory.SALES, ("pricing", "quote", "demo", "trial", "proposal")),
    (EmailCategory.CUSTOMER, ("bug", "broken", "error", "not working", "support", "help")),
]

_ACTION_PATTERNS = (
    re.compile(r"\b(please|can you|could you)\s+([^.?!\n]{5,80})", re.IGNORECASE),
    re.compile(r"\b(need|needs)\s+to\s+([^.?!\n]{5,80})", re.IGNORECASE),
)


def triage_email(email: EmailIn) -> EmailTriage:
    text = f"{email.subject}\n{email.body}".lower()
    category = EmailCategory.INTERNAL
    for cat, keywords in _TRIAGE_RULES:
        if any(k in text for k in keywords):
            category = cat
            break

    urgent = any(w in text for w in ("urgent", "asap", "outage", "down", "immediately"))
    if category in (EmailCategory.SPAM, EmailCategory.NEWSLETTER):
        priority = Priority.LOW
    elif urgent or category == EmailCategory.CUSTOMER:
        priority = Priority.HIGH
    else:
        priority = Priority.MEDIUM

    actions = []
    for pattern in _ACTION_PATTERNS:
        actions.extend(m.group(0).strip() for m in pattern.finditer(email.body))
    needs_reply = category in (EmailCategory.CUSTOMER, EmailCategory.SALES, EmailCategory.BILLING)
    return EmailTriage(
        category=category,
        priority=priority,
        summary=_first_sentences(email.body, 2) or email.subject or "(empty email)",
        action_items=actions[:8],
        needs_reply=needs_reply,
    )


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def html_to_text(raw: str) -> str:
    """Good-enough tag stripper so workflows can post raw HTML straight from an HTTP node."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw)).strip()


def summarise_page(page: PageIn) -> PageSummary:
    text = html_to_text(page.text) if "<" in page.text else re.sub(r"\s+", " ", page.text).strip()
    sentences = _split_sentences(text)
    summary = " ".join(sentences[:3])[:800] or "(no extractable text)"
    key_points = [s[:200] for s in sentences[3:8]]
    title = page.title or page.url.rstrip("/").rsplit("/", 1)[-1] or page.url
    return PageSummary(title=title[:200], summary=summary, key_points=key_points)


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _first_sentences(text: str, n: int) -> str:
    return " ".join(_split_sentences(re.sub(r"\s+", " ", text))[:n])[:400]
