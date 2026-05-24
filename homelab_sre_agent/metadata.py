from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class OperationalDependencyRule:
    dependency: str
    pattern: str
    reason: str


@dataclass(frozen=True)
class ServiceMetadata:
    name: str
    containers: tuple[str, ...]
    images: tuple[str, ...]
    source_repo: str | None
    source_path_hint: str | None
    issue_repo: str
    deploy_repo: str
    deploy_path: str | None
    runbook_url: str | None
    labels: tuple[str, ...] = field(default_factory=tuple)
    sre_enabled: bool = False
    autofix: bool = False
    repo_daily_limit: int = 1
    operational_dependency_rules: tuple[OperationalDependencyRule, ...] = field(default_factory=tuple)
    unknown: bool = False


@dataclass(frozen=True)
class ServiceCatalog:
    services: tuple[ServiceMetadata, ...]
    default_issue_repo: str

    def match(self, *, container_name: str, image: str) -> ServiceMetadata:
        for service in self.services:
            if container_name in service.containers:
                return service
        for service in self.services:
            if any(fnmatch(image, pattern) for pattern in service.images):
                return service
        return unknown_service(container_name, self.default_issue_repo)


def load_catalog(path: Path, *, default_issue_repo: str) -> ServiceCatalog:
    if not path.exists():
        return ServiceCatalog(services=(), default_issue_repo=default_issue_repo)

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    defaults = payload.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}
    fallback_issue_repo = str(defaults.get("issue_repo") or default_issue_repo)
    fallback_deploy_repo = str(defaults.get("deploy_repo") or "feocco/homelab-config")

    services_payload = payload.get("services") or {}
    if not isinstance(services_payload, dict):
        raise ValueError("services must be a mapping")

    services = []
    for name, raw in services_payload.items():
        if not isinstance(raw, dict):
            raise ValueError(f"service {name} must be a mapping")
        source = raw.get("source") or {}
        deploy = raw.get("deploy") or {}
        sre = raw.get("sre") or {}
        if not isinstance(source, dict):
            source = {}
        if not isinstance(deploy, dict):
            deploy = {}
        if not isinstance(sre, dict):
            sre = {}
        source_repo = optional_str(source.get("repo"))
        issue_repo = optional_str(raw.get("issue_repo")) or source_repo or fallback_issue_repo
        services.append(
            ServiceMetadata(
                name=str(name),
                containers=tuple_values(raw.get("containers"), default=(str(name),)),
                images=tuple_values(raw.get("images"), default=()),
                source_repo=source_repo,
                source_path_hint=optional_str(source.get("path_hint")),
                issue_repo=issue_repo,
                deploy_repo=optional_str(deploy.get("repo")) or fallback_deploy_repo,
                deploy_path=optional_str(deploy.get("path")),
                runbook_url=optional_str(raw.get("runbook_url")),
                labels=tuple_values(raw.get("labels"), default=("homelab-sre",)),
                sre_enabled=parse_bool(sre.get("enabled"), False),
                autofix=parse_bool(sre.get("autofix"), False),
                repo_daily_limit=parse_int(sre.get("repo_daily_limit"), 1),
                operational_dependency_rules=parse_operational_dependency_rules(sre.get("routing")),
            )
        )
    return ServiceCatalog(services=tuple(services), default_issue_repo=fallback_issue_repo)


def unknown_service(container_name: str, default_issue_repo: str) -> ServiceMetadata:
    return ServiceMetadata(
        name=f"unknown-{container_name or 'container'}",
        containers=(container_name,) if container_name else (),
        images=(),
        source_repo=None,
        source_path_hint=None,
        issue_repo=default_issue_repo,
        deploy_repo="feocco/homelab-config",
        deploy_path=None,
        runbook_url=None,
        labels=("homelab-sre", "unknown-service"),
        sre_enabled=False,
        autofix=False,
        repo_daily_limit=0,
        operational_dependency_rules=(),
        unknown=True,
    )


def parse_operational_dependency_rules(value: Any) -> tuple[OperationalDependencyRule, ...]:
    if not isinstance(value, dict):
        return ()
    raw_rules = value.get("operational_dependencies") or ()
    if not isinstance(raw_rules, list):
        return ()

    rules: list[OperationalDependencyRule] = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        dependency = optional_str(raw.get("dependency"))
        pattern = optional_str(raw.get("pattern"))
        if not dependency or not pattern:
            continue
        reason = optional_str(raw.get("reason")) or f"{dependency} matched an operational dependency routing rule."
        rules.append(OperationalDependencyRule(dependency=dependency, pattern=pattern, reason=reason))
    return tuple(rules)


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def tuple_values(value: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return default


def parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: Any, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError("integer values must be >= 0")
    return parsed
