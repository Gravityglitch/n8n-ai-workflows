from fastapi.testclient import TestClient

from sidecar.adapters import Adapters
from sidecar.api import create_app
from sidecar.config import Settings
from sidecar.llm import FixtureLLM
from sidecar.rules import html_to_text
from tests.conftest import TEST_API_KEY


def test_summarise_builds_markdown_report(client: TestClient, sample_pages: list[dict]) -> None:
    resp = client.post(
        "/summarise", json={"report_title": "Weekly research digest", "pages": sample_pages}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_title"] == "Weekly research digest"
    assert len(body["pages"]) == 3

    html_page = body["pages"][0]
    assert "<" not in html_page["summary"]
    assert "track()" not in html_page["summary"]
    assert "approval node" in html_page["summary"]
    assert len(html_page["key_points"]) >= 1

    untitled = body["pages"][1]
    assert untitled["title"] == "pricing-update"  # derived from the URL slug

    empty = body["pages"][2]
    assert empty["summary"] == "(no extractable text)"

    md = body["markdown"]
    assert md.startswith("# Weekly research digest")
    assert "## Release notes - October" in md
    assert "<https://example.com/blog/pricing-update>" in md
    assert md.endswith("\n")


def test_summarise_requires_at_least_one_page(client: TestClient) -> None:
    assert client.post("/summarise", json={"pages": []}).status_code == 422


def test_summarise_fallback(settings: Settings, sample_pages: list[dict]) -> None:
    app = create_app(
        settings, llm=FixtureLLM(fail_steps={"summarise"}), adapters=Adapters.dry_run()
    )
    with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as client:
        pages = client.post("/summarise", json={"pages": sample_pages[:1]}).json()["pages"]
    assert pages[0]["source"] == "rules"


def test_html_to_text_strips_scripts_and_tags() -> None:
    raw = "<div><script>alert(1)</script><p>Hello <b>world</b></p>  <style>.x{}</style></div>"
    assert html_to_text(raw) == "Hello world"
