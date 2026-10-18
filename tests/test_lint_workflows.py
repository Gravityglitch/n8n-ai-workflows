import json
from pathlib import Path

import pytest

from tests.conftest import FIXTURES, WORKFLOWS, load_json
from tools.lint_workflows import Issue, lint_file, lint_workflow, main

WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.json"))


def test_three_workflows_are_shipped() -> None:
    assert [p.name for p in WORKFLOW_FILES] == [
        "gmail_triage_digest.json",
        "lead_intake_ai_scoring.json",
        "scrape_summarise_report.json",
    ]


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_shipped_workflows_are_clean(path: Path) -> None:
    assert lint_file(path) == []


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_shipped_workflows_have_n8n_export_shape(path: Path) -> None:
    data = load_json(path)
    assert set(data) >= {"name", "nodes", "connections", "settings"}
    assert data["active"] is False
    sidecar_nodes = [
        n
        for n in data["nodes"]
        if n["type"] == "n8n-nodes-base.httpRequest"
        and "$env.SIDECAR_URL" in n["parameters"]["url"]
    ]
    assert sidecar_nodes, "every workflow calls the sidecar at least once"


def test_lead_workflow_gates_writes_behind_wait_nodes() -> None:
    data = load_json(WORKFLOWS / "lead_intake_ai_scoring.json")
    waits = [n for n in data["nodes"] if n["type"] == "n8n-nodes-base.wait"]
    assert len(waits) == 2
    assert all(n["parameters"]["resume"] == "webhook" for n in waits)
    actions = [
        n
        for n in data["nodes"]
        if n["type"] == "n8n-nodes-base.httpRequest" and n["parameters"]["url"].endswith("/actions")
    ]
    assert len(actions) == 2
    assert all("$execution.resumeUrl" in n["parameters"]["jsonBody"] for n in actions)


def test_broken_fixture_reports_every_class_of_problem() -> None:
    issues = lint_file(FIXTURES / "broken_workflow.json")
    messages = [i.message for i in issues if i.level == "error"]
    warnings = [i.message for i in issues if i.level == "warning"]

    def has(fragment: str) -> bool:
        return any(fragment in m for m in messages)

    assert has("webhook node has no path")
    assert has("webhook node has no webhookId")
    assert has("duplicate node name used 2 times")
    assert has("connection target 'Does Not Exist' is not a node")
    assert has("connection source 'Ghost' is not a node")
    assert has("hard-coded secret")
    assert has("header 'x-api-key' is a literal")
    assert has("header 'authorization' is a literal")
    assert has("sidecar call must build its url from {{ $env.SIDECAR_URL }}")
    assert has("sidecar call is missing the X-API-Key header")
    assert has("wait node resumed by webhook needs a webhookId")
    assert has("position must be [x, y]")
    assert has("unknown node type package")
    assert has("credential 'slackApi' must only reference id/name")
    assert has("missing 'typeVersion'")
    assert any("not connected" in w for w in warnings)


def test_top_level_shape_errors() -> None:
    assert lint_workflow([], "x.json")[0].message == "top level must be an object"
    issues = lint_workflow({"nodes": "nope"}, "x.json")
    assert {i.message for i in issues} >= {
        "missing or invalid top-level 'name' (expected str)",
        "missing or invalid top-level 'nodes' (expected list)",
    }


def test_nameless_and_non_object_nodes() -> None:
    issues = lint_workflow(
        {
            "name": "x",
            "nodes": ["str", {"type": "n8n-nodes-base.set"}],
            "connections": {},
            "settings": {},
        },
        "x.json",
    )
    assert {i.message for i in issues} >= {"node #0 is not an object", "node #1 has no name"}


def test_missing_trigger_is_a_warning() -> None:
    wf = {
        "name": "x",
        "nodes": [
            {"name": "A", "type": "n8n-nodes-base.set", "typeVersion": 3, "position": [0, 0]}
        ],
        "connections": {},
        "settings": {},
    }
    issues = lint_workflow(wf, "x.json")
    assert [i.level for i in issues] == ["warning"]
    assert "no trigger" in issues[0].message


def test_credential_managed_http_auth_is_accepted() -> None:
    wf = {
        "name": "x",
        "nodes": [
            {
                "name": "T",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
            },
            {
                "name": "H",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [1, 0],
                "parameters": {
                    "url": "https://api.example.com/score",
                    "authentication": "predefinedCredentialType",
                },
            },
        ],
        "connections": {"T": {"main": [[{"node": "H", "type": "main", "index": 0}]]}},
        "settings": {},
    }
    assert lint_workflow(wf, "x.json") == []


def test_http_node_without_url() -> None:
    wf = {
        "name": "x",
        "nodes": [
            {
                "name": "H",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4,
                "position": [0, 0],
            }
        ],
        "connections": {},
        "settings": {},
    }
    assert any(i.message == "http node has no url" for i in lint_workflow(wf, "x.json"))


def test_malformed_connections() -> None:
    wf = {
        "name": "x",
        "nodes": [
            {"name": "A", "type": "n8n-nodes-base.set", "typeVersion": 3, "position": [0, 0]}
        ],
        "connections": {"A": "bad"},
        "settings": {},
    }
    assert any("outputs must be an object" in i.message for i in lint_workflow(wf, "x.json"))
    wf["connections"] = {"A": {"main": "bad"}}
    assert any("must be a list of branches" in i.message for i in lint_workflow(wf, "x.json"))


def test_unparseable_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    issues = lint_file(bad)
    assert len(issues) == 1 and "cannot parse JSON" in issues[0].message
    assert str(issues[0]).startswith("ERROR")


def test_cli_passes_on_shipped_workflows(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(WORKFLOWS)]) == 0
    out = capsys.readouterr().out
    assert "3 file(s), 0 error(s), 0 warning(s)" in out


def test_cli_fails_on_broken_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURES / "broken_workflow.json")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL")


def test_cli_strict_turns_warnings_into_failure(tmp_path: Path) -> None:
    wf = {
        "name": "x",
        "nodes": [
            {"name": "A", "type": "n8n-nodes-base.set", "typeVersion": 3, "position": [0, 0]}
        ],
        "connections": {},
        "settings": {},
    }
    path = tmp_path / "w.json"
    path.write_text(json.dumps(wf))
    assert main([str(path)]) == 0
    assert main([str(path), "--strict"]) == 1


def test_cli_with_no_files(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 2


def test_issue_str_without_node() -> None:
    assert str(Issue("f.json", "warning", None, "m")) == "WARNING f.json: m"
