import pytest

from sidecar.config import Settings


def test_csv_api_keys_are_split() -> None:
    s = Settings(_env_file=None, api_keys="a, b,,c")
    assert s.api_keys == ["a", "b", "c"]
    assert s.auth_enabled


def test_defaults_are_offline_and_dry_run() -> None:
    s = Settings(_env_file=None)
    assert s.llm_backend == "fixture"
    assert (s.crm_mode, s.slack_mode, s.email_mode, s.n8n_callback_mode) == ("dry_run",) * 4


def test_production_requires_keys_and_tokens() -> None:
    with pytest.raises(ValueError) as exc:
        Settings(
            _env_file=None,
            app_env="production",
            llm_backend="claude",
            crm_mode="live",
            slack_mode="live",
            email_mode="live",
        )
    msg = str(exc.value)
    for needle in ("API_KEYS", "ANTHROPIC_API_KEY", "HUBSPOT", "SLACK_BOT_TOKEN", "GMAIL"):
        assert needle in msg


def test_production_valid_config() -> None:
    s = Settings(_env_file=None, app_env="production", api_keys="k", llm_backend="fixture")
    assert s.app_env == "production"
