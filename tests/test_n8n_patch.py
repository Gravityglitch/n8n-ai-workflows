import json
from pathlib import Path

import pytest

from tests.conftest import FIXTURES, WORKFLOWS, load_json
from tools.lint_workflows import lint_workflow
from tools.n8n_patch import PatchError, find_edge, insert_node_between, main

LEAD = WORKFLOWS / "lead_intake_ai_scoring.json"


@pytest.fixture
def workflow() -> dict:
    return load_json(LEAD)


@pytest.fixture
def dedupe_node() -> dict:
    return load_json(FIXTURES / "dedupe_node.json")


def test_insert_rewires_edge_and_keeps_everything_else(workflow: dict, dedupe_node: dict) -> None:
    before = json.dumps(workflow, sort_keys=True)
    patched = insert_node_between(workflow, "Normalise Lead", "Score Lead (sidecar)", dedupe_node)

    assert json.dumps(workflow, sort_keys=True) == before  # input untouched
    assert len(patched["nodes"]) == len(workflow["nodes"]) + 1
    assert patched["connections"]["Normalise Lead"]["main"][0] == [
        {"node": "Dedupe by Email", "type": "main", "index": 0}
    ]
    assert patched["connections"]["Dedupe by Email"]["main"][0] == [
        {"node": "Score Lead (sidecar)", "type": "main", "index": 0}
    ]
    # all other connections untouched
    for source, outputs in workflow["connections"].items():
        if source != "Normalise Lead":
            assert patched["connections"][source] == outputs

    new = next(n for n in patched["nodes"] if n["name"] == "Dedupe by Email")
    assert new["position"] == [330, 300]  # midpoint of the two neighbours
    assert lint_workflow(patched, "patched.json") == []


def test_insert_on_second_output_of_if_node(workflow: dict) -> None:
    node = {
        "name": "Tag Nurture",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "position": [1, 2],
    }
    patched = insert_node_between(workflow, "Qualified?", "Request Nurture Approval", node)
    branches = patched["connections"]["Qualified?"]["main"]
    assert branches[0][0]["node"] == "Request CRM Approval"  # true branch untouched
    assert branches[1][0]["node"] == "Tag Nurture"
    assert patched["connections"]["Tag Nurture"]["main"][0][0]["node"] == "Request Nurture Approval"
    assert next(n for n in patched["nodes"] if n["name"] == "Tag Nurture")["position"] == [1, 2]


def test_find_edge(workflow: dict) -> None:
    assert find_edge(workflow, "Qualified?", "Request Nurture Approval") == (1, 0)
    with pytest.raises(PatchError, match="no main connection"):
        find_edge(workflow, "Qualified?", "Confirm in Slack")


@pytest.mark.parametrize(
    ("after", "before", "match"),
    [
        ("Nope", "Score Lead (sidecar)", "not found"),
        ("Normalise Lead", "Confirm in Slack", "no main connection"),
    ],
)
def test_refuses_bad_edges(
    workflow: dict, dedupe_node: dict, after: str, before: str, match: str
) -> None:
    with pytest.raises(PatchError, match=match):
        insert_node_between(workflow, after, before, dedupe_node)


def test_refuses_duplicate_name_and_incomplete_node(workflow: dict) -> None:
    dup = {"name": "Qualified?", "type": "n8n-nodes-base.set", "typeVersion": 3.4}
    with pytest.raises(PatchError, match="already exists"):
        insert_node_between(workflow, "Normalise Lead", "Score Lead (sidecar)", dup)
    with pytest.raises(PatchError, match="missing 'type'"):
        insert_node_between(workflow, "Normalise Lead", "Score Lead (sidecar)", {"name": "X"})


def test_refuses_ambiguous_names(dedupe_node: dict) -> None:
    wf = {
        "name": "x",
        "nodes": [
            {"name": "A", "type": "t", "typeVersion": 1, "position": [0, 0]},
            {"name": "A", "type": "t", "typeVersion": 1, "position": [0, 0]},
            {"name": "B", "type": "t", "typeVersion": 1, "position": [0, 0]},
        ],
        "connections": {"A": {"main": [[{"node": "B", "type": "main", "index": 0}]]}},
    }
    with pytest.raises(PatchError, match="ambiguous"):
        insert_node_between(wf, "A", "B", dedupe_node)


def test_cli_writes_out_file(tmp_path: Path) -> None:
    out = tmp_path / "patched.json"
    code = main(
        [
            str(LEAD),
            "--after",
            "Normalise Lead",
            "--before",
            "Score Lead (sidecar)",
            "--node-file",
            str(FIXTURES / "dedupe_node.json"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert "Dedupe by Email" in load_json(out)["connections"]


def test_cli_in_place_keeps_backup(tmp_path: Path) -> None:
    target = tmp_path / "wf.json"
    target.write_text(LEAD.read_text())
    code = main(
        [
            str(target),
            "--after",
            "Normalise Lead",
            "--before",
            "Score Lead (sidecar)",
            "--node-file",
            str(FIXTURES / "dedupe_node.json"),
            "--in-place",
        ]
    )
    assert code == 0
    assert (tmp_path / "wf.json.bak").read_text() == LEAD.read_text()
    assert "Dedupe by Email" in load_json(target)["connections"]


def test_cli_stdout_and_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            str(LEAD),
            "--after",
            "Normalise Lead",
            "--before",
            "Score Lead (sidecar)",
            "--node-file",
            str(FIXTURES / "dedupe_node.json"),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["name"]
    code = main(
        [
            str(LEAD),
            "--after",
            "Normalise Lead",
            "--before",
            "Nope",
            "--node-file",
            str(FIXTURES / "dedupe_node.json"),
        ]
    )
    assert code == 1
    assert "error:" in capsys.readouterr().err
