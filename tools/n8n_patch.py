"""Safely extend an existing n8n workflow: insert a node between two connected nodes.

This is the operation most "fix/extend my n8n workflow" jobs reduce to: add a validation,
transform, approval or logging step in the middle of a client's flow without re-wiring it by
hand in the editor and without touching anything else in the export.

    python -m tools.n8n_patch workflows/lead_intake_ai_scoring.json \
        --after "Normalise Lead" --before "Score Lead (sidecar)" \
        --node-file examples/dedupe_node.json --out patched.json

The patch is pure: it returns a new dict, keeps every other node and connection byte-identical,
and refuses to run if the names are ambiguous or the edge does not exist.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any


class PatchError(ValueError):
    pass


def _node_by_name(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [n for n in workflow.get("nodes", []) if n.get("name") == name]
    if not matches:
        raise PatchError(f"node {name!r} not found")
    if len(matches) > 1:
        raise PatchError(f"node name {name!r} is ambiguous ({len(matches)} nodes)")
    return matches[0]


def find_edge(workflow: dict[str, Any], after: str, before: str) -> tuple[int, int]:
    """Return (output_index, position_in_branch) of the main connection after -> before."""
    branches = workflow.get("connections", {}).get(after, {}).get("main", [])
    for output_index, branch in enumerate(branches):
        for pos, target in enumerate(branch or []):
            if target.get("node") == before:
                return output_index, pos
    raise PatchError(f"no main connection from {after!r} to {before!r}")


def insert_node_between(
    workflow: dict[str, Any], after: str, before: str, new_node: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of `workflow` with `new_node` wired as after -> new_node -> before."""
    for key in ("name", "type", "typeVersion"):
        if key not in new_node:
            raise PatchError(f"new node is missing {key!r}")
    new_name = new_node["name"]
    if any(n.get("name") == new_name for n in workflow.get("nodes", [])):
        raise PatchError(f"a node named {new_name!r} already exists")

    a = _node_by_name(workflow, after)
    b = _node_by_name(workflow, before)
    output_index, pos = find_edge(workflow, after, before)

    patched = copy.deepcopy(workflow)
    node = copy.deepcopy(new_node)
    node.setdefault("parameters", {})
    if "position" not in node:
        ax, ay = a.get("position", [0, 0])
        bx, by = b.get("position", [ax + 400, ay])
        node["position"] = [round((ax + bx) / 2), round((ay + by) / 2)]
    patched["nodes"].append(node)

    # Re-point the existing edge at the new node, keeping its slot in the branch.
    old_target = patched["connections"][after]["main"][output_index][pos]
    patched["connections"][after]["main"][output_index][pos] = {**old_target, "node": new_name}
    patched["connections"][new_name] = {
        "main": [[{"node": before, "type": "main", "index": old_target.get("index", 0)}]]
    }
    return patched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("workflow", type=Path)
    parser.add_argument("--after", required=True, help="name of the upstream node")
    parser.add_argument("--before", required=True, help="name of the downstream node")
    parser.add_argument(
        "--node-file", required=True, type=Path, help="JSON file with the node to insert"
    )
    parser.add_argument("--out", type=Path, help="write here (default: stdout)")
    parser.add_argument(
        "--in-place", action="store_true", help="overwrite the workflow, keeping a .bak"
    )
    args = parser.parse_args(argv)

    workflow = json.loads(args.workflow.read_text(encoding="utf-8"))
    new_node = json.loads(args.node_file.read_text(encoding="utf-8"))
    try:
        patched = insert_node_between(workflow, args.after, args.before, new_node)
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(patched, indent=2, ensure_ascii=False) + "\n"
    if args.in_place:
        backup = args.workflow.with_suffix(args.workflow.suffix + ".bak")
        backup.write_text(args.workflow.read_text(encoding="utf-8"), encoding="utf-8")
        args.workflow.write_text(text, encoding="utf-8")
        print(f"patched {args.workflow} (backup at {backup})", file=sys.stderr)
    elif args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
