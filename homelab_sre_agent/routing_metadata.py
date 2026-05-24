from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import yaml


REQUIRED_FIELDS = ("source_issue_url", "service", "dependency", "pattern", "reason", "evidence")
SUGGESTION_MARKER = "Operational routing metadata suggestion:"


class RoutingMetadataError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutingSuggestion:
    source_issue_url: str
    service: str
    dependency: str
    pattern: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class ApplyResult:
    changed: bool
    message: str


def load_suggestion(text: str) -> RoutingSuggestion:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingMetadataError(f"invalid suggestion yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise RoutingMetadataError("suggestion must be a yaml mapping")

    values = {}
    for field in REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RoutingMetadataError(f"missing required field: {field}")
        values[field] = value.strip()

    return RoutingSuggestion(**values)


def extract_latest_suggestion(comments: Iterable[dict[str, Any]]) -> RoutingSuggestion:
    for comment in reversed(list(comments)):
        body = str(comment.get("body") or "")
        if SUGGESTION_MARKER not in body:
            continue
        for match in re.finditer(r"```(?:yaml|yml)\n(.*?)\n```", body, flags=re.DOTALL | re.IGNORECASE):
            try:
                return load_suggestion(match.group(1))
            except RoutingMetadataError:
                continue
    raise RoutingMetadataError("no valid routing metadata suggestion found")


def apply_routing_metadata(*, services_path: Path, suggestion: RoutingSuggestion) -> ApplyResult:
    original = services_path.read_text(encoding="utf-8")
    parsed = _load_services(original)
    services = parsed.get("services")
    if not isinstance(services, dict):
        raise RoutingMetadataError("services.yaml must contain a services mapping")
    service = services.get(suggestion.service)
    if not isinstance(service, dict):
        raise RoutingMetadataError(f"unknown service: {suggestion.service}")

    rules = (((service.get("sre") or {}).get("routing") or {}).get("operational_dependencies") or [])
    if not isinstance(rules, list):
        raise RoutingMetadataError(f"{suggestion.service} operational_dependencies must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if (
            rule.get("dependency") == suggestion.dependency
            and rule.get("pattern") == suggestion.pattern
        ):
            return ApplyResult(changed=False, message="routing metadata already exists")

    updated = _insert_rule(original, suggestion)
    services_path.write_text(updated, encoding="utf-8")
    return ApplyResult(changed=True, message="routing metadata added")


def load_suggestion_from_issue_json(path: Path) -> RoutingSuggestion:
    raw = json.loads(path.read_text(encoding="utf-8"))
    comments = raw.get("comments")
    if isinstance(comments, list):
        return extract_latest_suggestion(comments)
    raise RoutingMetadataError("issue json must contain comments list")


def _load_services(text: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingMetadataError(f"invalid services yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise RoutingMetadataError("services.yaml must be a yaml mapping")
    return raw


def _insert_rule(text: str, suggestion: RoutingSuggestion) -> str:
    lines = text.splitlines()
    service_index = _find_service_index(lines, suggestion.service)
    service_indent = _indent_width(lines[service_index])
    service_end = _find_block_end(lines, service_index + 1, service_indent)
    sre_index = _find_child_key(lines, service_index + 1, service_end, service_indent + 2, "sre")
    rule_lines = _format_rule(suggestion)

    if sre_index is None:
        insert_at = service_end
        block = [
            " " * (service_indent + 2) + "sre:",
            " " * (service_indent + 4) + "routing:",
            " " * (service_indent + 6) + "operational_dependencies:",
            *[" " * (service_indent + 8) + line for line in rule_lines],
        ]
        return _insert_lines(lines, insert_at, block)

    sre_indent = _indent_width(lines[sre_index])
    sre_end = _find_block_end(lines, sre_index + 1, sre_indent)
    routing_index = _find_child_key(lines, sre_index + 1, sre_end, sre_indent + 2, "routing")
    if routing_index is None:
        insert_at = sre_end
        block = [
            " " * (sre_indent + 2) + "routing:",
            " " * (sre_indent + 4) + "operational_dependencies:",
            *[" " * (sre_indent + 6) + line for line in rule_lines],
        ]
        return _insert_lines(lines, insert_at, block)

    routing_indent = _indent_width(lines[routing_index])
    routing_end = _find_block_end(lines, routing_index + 1, routing_indent)
    dependencies_index = _find_child_key(
        lines, routing_index + 1, routing_end, routing_indent + 2, "operational_dependencies"
    )
    if dependencies_index is None:
        insert_at = routing_end
        block = [
            " " * (routing_indent + 2) + "operational_dependencies:",
            *[" " * (routing_indent + 4) + line for line in rule_lines],
        ]
        return _insert_lines(lines, insert_at, block)

    dependencies_indent = _indent_width(lines[dependencies_index])
    dependencies_end = _find_block_end(lines, dependencies_index + 1, dependencies_indent)
    block = [" " * (dependencies_indent + 2) + line for line in rule_lines]
    return _insert_lines(lines, dependencies_end, block)


def _format_rule(suggestion: RoutingSuggestion) -> list[str]:
    return [
        f"- dependency: {_quote_yaml(suggestion.dependency)}",
        f"  pattern: {_quote_yaml(suggestion.pattern)}",
        f"  reason: {_quote_yaml(suggestion.reason)}",
    ]


def _quote_yaml(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        return value
    return json.dumps(value)


def _find_service_index(lines: list[str], service: str) -> int:
    pattern = re.compile(rf"^  {re.escape(service)}:\s*$")
    for index, line in enumerate(lines):
        if pattern.match(line):
            return index
    raise RoutingMetadataError(f"unknown service: {service}")


def _find_child_key(lines: list[str], start: int, end: int, indent: int, key: str) -> int | None:
    pattern = re.compile(rf"^ {{{indent}}}{re.escape(key)}:\s*(?:#.*)?$")
    for index in range(start, end):
        if pattern.match(lines[index]):
            return index
    return None


def _find_block_end(lines: list[str], start: int, parent_indent: int) -> int:
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if _indent_width(line) <= parent_indent:
            return index
    return len(lines)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _insert_lines(lines: list[str], index: int, new_lines: list[str]) -> str:
    updated = lines[:index] + new_lines + lines[index:]
    return "\n".join(updated) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply reviewed SRE routing metadata to homelab-config services.yaml.")
    parser.add_argument("--services-yaml", required=True, type=Path)
    parser.add_argument("--suggestion-file", type=Path)
    parser.add_argument("--issue-json", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.suggestion_file:
            suggestion = load_suggestion(args.suggestion_file.read_text(encoding="utf-8"))
        elif args.issue_json:
            suggestion = load_suggestion_from_issue_json(args.issue_json)
        else:
            raise RoutingMetadataError("provide --suggestion-file or --issue-json")
        result = apply_routing_metadata(services_path=args.services_yaml, suggestion=suggestion)
    except RoutingMetadataError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
