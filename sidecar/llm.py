"""LLM adapter: one structured-output call, two backends.

ClaudeLLM   - Anthropic SDK, `messages.parse` with a Pydantic output schema, tenacity retries.
FixtureLLM  - deterministic, offline; used by tests, CI and the default config (no API key).

Both implement `LLM.structured(step, system, user, schema) -> schema instance`.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from sidecar import rules
from sidecar.models import EmailIn, EmailTriage, Lead, LeadScore, PageIn, PageSummary

log = structlog.get_logger(__name__)

SCORE_SYSTEM = """You score inbound B2B leads for a software consultancy.
Return a 0-100 score, a tier (hot >= 70, warm >= 40, cold otherwise), up to six short reasons
grounded only in the fields provided, and one concrete next step. Never invent facts."""

TRIAGE_SYSTEM = """You triage a shared inbox for a small software company.
Classify the email, set a priority, write a one- or two-sentence summary, list concrete action
items (imperative, max 8) and say whether a human reply is needed."""

SUMMARISE_SYSTEM = """You summarise web pages for a weekly research digest.
Produce a faithful 2-4 sentence summary and up to eight key points. Do not add information that
is not on the page."""


class LLMError(RuntimeError):
    """Raised when the model backend fails after retries or returns unusable output."""


class LLM(Protocol):
    name: str

    def structured[T: BaseModel](self, step: str, system: str, user: str, schema: type[T]) -> T: ...


def _is_retryable(exc: BaseException) -> bool:
    import anthropic

    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


class ClaudeLLM:
    name = "claude"

    def __init__(
        self,
        model: str = "claude-sonnet-5-5",
        client: Any | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        api_key: str | None = None,
    ) -> None:
        import anthropic

        self.model = model
        # SDK retries disabled so tenacity owns backoff and the retry count.
        self.client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self._parse = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max(1, max_retries)),
            wait=wait_exponential_jitter(initial=1, max=20),
            reraise=True,
        )(self._parse_once)

    def _parse_once(self, step: str, **kwargs: Any) -> Any:
        response = self.client.messages.parse(model=self.model, **kwargs)
        usage = getattr(response, "usage", None)
        log.info(
            "llm.usage",
            step=step,
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            stop_reason=getattr(response, "stop_reason", None),
        )
        return response

    def structured[T: BaseModel](self, step: str, system: str, user: str, schema: type[T]) -> T:
        response = self._parse(
            step,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"{step}: output not parsed (stop_reason={response.stop_reason})")
        return parsed


class FixtureLLM:
    """Offline stand-in. Produces the same schemas as Claude from the deterministic rule engine.

    `fail_steps` lets tests simulate an outage for specific steps so the fallback path is exercised.
    """

    name = "fixture"

    def __init__(self, fail_steps: set[str] | None = None) -> None:
        self.fail_steps = fail_steps or set()
        self.calls: list[str] = []

    def structured[T: BaseModel](self, step: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(step)
        if step in self.fail_steps:
            raise LLMError(f"fixture configured to fail step {step!r}")
        if schema is LeadScore:
            return rules.score_lead(Lead.model_validate_json(user))  # type: ignore[return-value]
        if schema is EmailTriage:
            return rules.triage_email(EmailIn.model_validate_json(user))  # type: ignore[return-value]
        if schema is PageSummary:
            return rules.summarise_page(PageIn.model_validate_json(user))  # type: ignore[return-value]
        raise LLMError(f"fixture has no generator for {schema.__name__}")
