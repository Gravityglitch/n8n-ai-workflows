import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidecar.adapters import Adapters
from sidecar.api import create_app
from sidecar.config import Settings
from sidecar.llm import FixtureLLM

TEST_API_KEY = "test-key"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WORKFLOWS = ROOT / "workflows"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A developer's real .env must never leak into the test run.
os.environ.pop("ANTHROPIC_API_KEY", None)


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        api_keys=[TEST_API_KEY],
        database_url="sqlite://",
        llm_backend="fixture",
        log_json=False,
    )


@pytest.fixture
def llm() -> FixtureLLM:
    return FixtureLLM()


@pytest.fixture
def adapters() -> Adapters:
    return Adapters.dry_run()


@pytest.fixture
def client(settings: Settings, llm: FixtureLLM, adapters: Adapters) -> TestClient:
    with TestClient(
        create_app(settings, llm=llm, adapters=adapters), headers={"X-API-Key": TEST_API_KEY}
    ) as c:
        yield c


@pytest.fixture
def sample_leads() -> list[dict]:
    return load_json(DATA / "sample_leads.json")


@pytest.fixture
def sample_emails() -> list[dict]:
    return load_json(DATA / "sample_emails.json")


@pytest.fixture
def sample_pages() -> list[dict]:
    return load_json(DATA / "sample_pages.json")
