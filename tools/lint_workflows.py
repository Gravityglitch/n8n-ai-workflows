"""Lint n8n workflow exports before they are imported into a client's instance.

Checks (error unless noted):
  - top-level shape: name, nodes (list), connections (dict), settings (dict)
  - every node has name/type/typeVersion/position; names are unique
  - node types come from a known package prefix
  - every connection source and target names an existing node
  - webhook and wait(resume=webhook) nodes have a path / webhookId
  - no hard-coded credentials: secret-looking strings, auth headers that are not expressions,
    credential blocks with anything beyond id/name
  - every HTTP Request node pointing at the sidecar sends X-API-Key from an env expression,
    and its URL is built from $env.SIDECAR_URL (no hard-coded host)
  - warning: nodes with no connections at all, no trigger node

Usage: python -m tools.lint_workflows workflows/*.json [--strict]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KNOWN_TYPE_PREFIXES = ("n8n-nodes-base.", "@n8n/n8n-nodes-langchain.")
TRIGGER_TYPES = (
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.cron",
)
SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "apikey", "x-auth-token"}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"pat-[a-z0-9-]{10,}", re.IGNORECASE),  # HubSpot private app tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}"),
)
SIDECAR_URL_MARKER = "$env.SIDECAR_URL"
API_KEY_ENV_MARKER = "$env.SIDECAR_API_KEY"


@dataclass(frozen=True)
class Issue:
    file: str
    level: str  # "error" | "warning"
    node: str | None
    message: str

    def __str__(self) -> str:
        where = f"{self.file}" + (f" [{self.node}]" if self.node else "")
        return f"{self.level.upper():7} {where}: {self.message}"


def _is_expression(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=") and "{{" in value


def _walk_strings(value: Any):  # noqa: ANN202 - generator of (path, str)
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def lint_workflow(data: Any, file: str) -> list[Issue]:
    issues: list[Issue] = []

    def err(msg: str, node: str | None = None) -> None:
        issues.append(Issue(file, "error", node, msg))

    def warn(msg: str, node: str | None = None) -> None:
        issues.append(Issue(file, "warning", node, msg))

    if not isinstance(data, dict):
        err("top level must be an object")
        return issues
    for key, typ in (("name", str), ("nodes", list), ("connections", dict), ("settings", dict)):
        if not isinstance(data.get(key), typ):
            err(f"missing or invalid top-level '{key}' (expected {typ.__name__})")
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    connections = data.get("connections") if isinstance(data.get("connections"), dict) else {}

    names: dict[str, int] = {}
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            err(f"node #{i} is not an object")
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            err(f"node #{i} has no name")
            continue
        names[name] = names.get(name, 0) + 1
        for key in ("type", "typeVersion", "position"):
            if key not in node:
                err(f"missing '{key}'", name)
        node_type = node.get("type", "")
        if isinstance(node_type, str) and not node_type.startswith(KNOWN_TYPE_PREFIXES):
            err(f"unknown node type package: {node_type!r}", name)
        pos = node.get("position")
        if not (
            isinstance(pos, list) and len(pos) == 2 and all(isinstance(p, int | float) for p in pos)
        ):
            err("position must be [x, y]", name)
        issues.extend(_lint_node(node, file))
    for name, count in names.items():
        if count > 1:
            err(f"duplicate node name used {count} times", name)

    # connections -----------------------------------------------------------
    linked: set[str] = set()
    for source, outputs in connections.items():
        if source not in names:
            err(f"connection source '{source}' is not a node")
            continue
        linked.add(source)
        if not isinstance(outputs, dict):
            err("connection outputs must be an object", source)
            continue
        for conn_type, branches in outputs.items():
            if not isinstance(branches, list):
                err(f"'{conn_type}' outputs must be a list of branches", source)
                continue
            for branch in branches:
                for target in branch or []:
                    tname = target.get("node") if isinstance(target, dict) else None
                    if tname not in names:
                        err(f"connection target '{tname}' is not a node", source)
                    else:
                        linked.add(tname)

    if nodes:
        for name in names:
            if name not in linked and len(names) > 1:
                warn("node is not connected to anything", name)
        if not any(n.get("type") in TRIGGER_TYPES for n in nodes if isinstance(n, dict)):
            warn("no trigger node found (webhook / schedule / manual)")

    return issues


def _lint_node(node: dict[str, Any], file: str) -> list[Issue]:
    issues: list[Issue] = []
    name = node["name"]
    node_type = node.get("type", "")
    params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}

    def err(msg: str) -> None:
        issues.append(Issue(file, "error", name, msg))

    # secrets anywhere in the node -----------------------------------------
    for text in _walk_strings(node):
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                err(f"hard-coded secret matches {pattern.pattern!r}")
                break

    creds = node.get("credentials")
    if isinstance(creds, dict):
        for cred_type, ref in creds.items():
            if not isinstance(ref, dict) or set(ref) - {"id", "name"}:
                err(f"credential '{cred_type}' must only reference id/name, never values")

    # webhooks --------------------------------------------------------------
    if node_type == "n8n-nodes-base.webhook":
        if not isinstance(params.get("path"), str) or not params["path"].strip():
            err("webhook node has no path")
        if not node.get("webhookId"):
            err("webhook node has no webhookId")
    if node_type == "n8n-nodes-base.wait" and params.get("resume") == "webhook":
        if not node.get("webhookId"):
            err("wait node resumed by webhook needs a webhookId")

    # http request ----------------------------------------------------------
    if node_type == "n8n-nodes-base.httpRequest":
        issues.extend(_lint_http_node(node, params, file))
    return issues


def _lint_http_node(node: dict[str, Any], params: dict[str, Any], file: str) -> list[Issue]:
    issues: list[Issue] = []
    name = node["name"]

    def err(msg: str) -> None:
        issues.append(Issue(file, "error", name, msg))

    url = params.get("url", "")
    if not isinstance(url, str) or not url:
        err("http node has no url")
        return issues

    headers = params.get("headerParameters", {}).get("parameters", [])
    header_map = {
        str(h.get("name", "")).lower(): h.get("value", "") for h in headers if isinstance(h, dict)
    }
    for hname, hvalue in header_map.items():
        if hname in SENSITIVE_HEADERS and not _is_expression(hvalue):
            err(f"header '{hname}' is a literal; use an expression such as ={{{{ $env.X }}}}")

    if params.get("authentication") in ("genericCredentialType", "predefinedCredentialType"):
        return issues  # credential-managed auth, nothing more to check

    targets_sidecar = SIDECAR_URL_MARKER in url or any(
        hint in url for hint in ("/score", "/triage", "/summarise", "/actions", "/approve")
    )
    if not targets_sidecar:
        return issues
    if SIDECAR_URL_MARKER not in url:
        err(f"sidecar call must build its url from {{{{ {SIDECAR_URL_MARKER} }}}}, got {url!r}")
    api_key = header_map.get("x-api-key")
    if api_key is None:
        err("sidecar call is missing the X-API-Key header")
    elif API_KEY_ENV_MARKER not in str(api_key):
        err(f"X-API-Key must come from {{{{ {API_KEY_ENV_MARKER} }}}}, got {api_key!r}")
    return issues


def lint_file(path: Path) -> list[Issue]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Issue(str(path), "error", None, f"cannot parse JSON: {exc}")]
    return lint_workflow(data, str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="+", type=Path, help="workflow JSON files or directories")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args(argv)

    files: list[Path] = []
    for p in args.paths:
        files.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    if not files:
        print("no workflow files found", file=sys.stderr)
        return 2

    all_issues: list[Issue] = []
    for file in files:
        issues = lint_file(file)
        all_issues.extend(issues)
        status = "FAIL" if any(i.level == "error" for i in issues) else "ok"
        print(f"{status:4} {file} ({len(issues)} issue(s))")
    for issue in all_issues:
        print(f"  {issue}")

    errors = sum(i.level == "error" for i in all_issues)
    warnings = sum(i.level == "warning" for i in all_issues)
    print(f"{len(files)} file(s), {errors} error(s), {warnings} warning(s)")
    failed = errors > 0 or (args.strict and warnings > 0)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
