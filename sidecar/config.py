"""Runtime configuration, validated once at startup via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLMBackend = Literal["claude", "fixture"]
Mode = Literal["dry_run", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---------------------------------------------------------------
    app_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- auth --------------------------------------------------------------
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated X-API-Key values accepted on every endpoint except /health.",
    )

    # --- persistence -------------------------------------------------------
    database_url: str = "sqlite:///./sidecar.db"

    # --- llm ---------------------------------------------------------------
    llm_backend: LLMBackend = "fixture"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3

    # --- scoring -----------------------------------------------------------
    score_threshold: int = Field(default=70, ge=0, le=100)

    # --- outbound adapters -------------------------------------------------
    crm_mode: Mode = "dry_run"
    hubspot_access_token: str | None = None
    slack_mode: Mode = "dry_run"
    slack_bot_token: str | None = None
    email_mode: Mode = "dry_run"
    gmail_access_token: str | None = None
    email_from: str = "ops@example.test"
    n8n_callback_mode: Mode = "dry_run"

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if not self.api_keys:
            problems.append("API_KEYS must be set")
        if self.llm_backend == "claude" and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY required for LLM_BACKEND=claude")
        if self.crm_mode == "live" and not self.hubspot_access_token:
            problems.append("HUBSPOT_ACCESS_TOKEN required for CRM_MODE=live")
        if self.slack_mode == "live" and not self.slack_bot_token:
            problems.append("SLACK_BOT_TOKEN required for SLACK_MODE=live")
        if self.email_mode == "live" and not self.gmail_access_token:
            problems.append("GMAIL_ACCESS_TOKEN required for EMAIL_MODE=live")
        if problems:
            raise ValueError("production config invalid: " + "; ".join(problems))
        return self

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
