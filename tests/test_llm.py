"""ClaudeLLM retry/parse behaviour with a scripted SDK stand-in; FixtureLLM edge cases."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from sidecar.api import build_llm
from sidecar.config import Settings
from sidecar.llm import SCORE_SYSTEM, ClaudeLLM, FixtureLLM, LLMError
from sidecar.models import LeadScore, Tier


class FlakyMessages:
    def __init__(self, failures: list[Exception], final: object) -> None:
        self.failures = failures
        self.final = final
        self.calls = 0

    def parse(self, **kwargs: object) -> object:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.final


def _status_error(code: int) -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError("boom", response=httpx.Response(code, request=req), body=None)


def _llm(messages: FlakyMessages, retries: int = 3) -> ClaudeLLM:
    llm = ClaudeLLM(client=SimpleNamespace(messages=messages), max_retries=retries)
    llm._parse.retry.wait = lambda *_: 0  # type: ignore[attr-defined]
    return llm


def _ok() -> SimpleNamespace:
    score = LeadScore(score=80, tier=Tier.HOT, reasons=["x"], suggested_next_step="call")
    usage = SimpleNamespace(input_tokens=100, output_tokens=20)
    return SimpleNamespace(stop_reason="end_turn", parsed_output=score, usage=usage)


def test_retries_transient_errors_then_succeeds() -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    messages = FlakyMessages([anthropic.APIConnectionError(request=req), _status_error(503)], _ok())
    result = _llm(messages).structured("score", SCORE_SYSTEM, "{}", LeadScore)
    assert result.score == 80
    assert messages.calls == 3


def test_rate_limit_is_retried() -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    messages = FlakyMessages([anthropic.RateLimitError("slow", response=resp, body=None)], _ok())
    assert _llm(messages).structured("score", SCORE_SYSTEM, "{}", LeadScore).tier == Tier.HOT
    assert messages.calls == 2


def test_does_not_retry_client_errors() -> None:
    messages = FlakyMessages([_status_error(400)], _ok())
    with pytest.raises(anthropic.APIStatusError):
        _llm(messages).structured("score", SCORE_SYSTEM, "{}", LeadScore)
    assert messages.calls == 1


def test_gives_up_after_max_retries() -> None:
    messages = FlakyMessages([_status_error(500)] * 5, _ok())
    with pytest.raises(anthropic.APIStatusError):
        _llm(messages, retries=2).structured("score", SCORE_SYSTEM, "{}", LeadScore)
    assert messages.calls == 2


def test_unparsed_output_raises_llm_error() -> None:
    bad = SimpleNamespace(stop_reason="max_tokens", parsed_output=None, usage=None)
    with pytest.raises(LLMError, match="not parsed"):
        _llm(FlakyMessages([], bad)).structured("score", SCORE_SYSTEM, "{}", LeadScore)


def test_fixture_rejects_unknown_schema() -> None:
    class Other(BaseModel):
        x: int

    with pytest.raises(LLMError, match="no generator"):
        FixtureLLM().structured("other", "", "{}", Other)


def test_build_llm_falls_back_to_fixture_without_key() -> None:
    settings = Settings(_env_file=None, llm_backend="claude", anthropic_api_key=None)
    assert isinstance(build_llm(settings), FixtureLLM)


def test_build_llm_uses_claude_with_key() -> None:
    settings = Settings(_env_file=None, llm_backend="claude", anthropic_api_key="sk-ant-test")
    llm = build_llm(settings)
    assert isinstance(llm, ClaudeLLM)
    assert llm.model == settings.llm_model
