from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .docker_logs import DockerLogCollector
from .github import GitHubClient, IssueResult
from .metadata import ServiceCatalog, ServiceMetadata
from .redact import redact_text
from .state import StateStore, utc_now


LOGGER = logging.getLogger("homelab-sre-agent")
MAX_PUBLIC_LINE_CHARS = 1000
EXCEPTION_RE = re.compile(r"\b([A-Za-z_][\w.]*Error|Exception):\s+(.+)")


@dataclass(frozen=True)
class Incident:
    container_id: str | None
    container_name: str
    image: str
    severity: str
    matched_pattern: str
    line: str
    normalized_line: str
    fingerprint: str
    occurred_at: str
    detected_at: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Incident":
        incident = payload.get("incident")
        if not isinstance(incident, dict):
            raise ValueError("payload.incident must be an object")
        container_name = str(incident.get("container_name") or "").strip()
        fingerprint = str(incident.get("fingerprint") or "").strip()
        if not container_name:
            raise ValueError("incident.container_name is required")
        if not fingerprint:
            raise ValueError("incident.fingerprint is required")
        return cls(
            container_id=optional_str(incident.get("container_id")),
            container_name=container_name,
            image=str(incident.get("image") or "unknown"),
            severity=str(incident.get("severity") or "ERROR"),
            matched_pattern=str(incident.get("matched_pattern") or ""),
            line=str(incident.get("line") or ""),
            normalized_line=str(incident.get("normalized_line") or ""),
            fingerprint=fingerprint,
            occurred_at=str(incident.get("occurred_at") or ""),
            detected_at=str(payload.get("detected_at") or ""),
        )


@dataclass(frozen=True)
class DiagnosticBundle:
    path: str
    reference: str


@dataclass(frozen=True)
class IssueAnalysis:
    summary: str
    observed: str
    expected: str
    representative_line: str


class SREService:
    def __init__(
        self,
        *,
        config: Config,
        catalog: ServiceCatalog | None = None,
        catalog_loader: Callable[[], ServiceCatalog] | None = None,
        state: StateStore,
        github: GitHubClient,
        logs: DockerLogCollector,
    ) -> None:
        self.config = config
        if catalog is None and catalog_loader is None:
            raise ValueError("catalog or catalog_loader is required")
        self.catalog = catalog
        self.catalog_loader = catalog_loader
        self.state = state
        self.github = github
        self.logs = logs

    def handle_incident(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        incident = Incident.from_payload(payload)
        service = self._catalog().match(container_name=incident.container_name, image=incident.image)

        if not service.sre_enabled:
            return {"ok": True, "status": "ignored", "reason": "sre disabled", "service": service.name}

        raw_logs = self._collect_logs(service, incident, now)
        bundle = self._write_diagnostic_bundle(service, incident, raw_logs, now)
        analysis = analyze_incident(incident, raw_logs)
        sanitized_line = redact_text(analysis.representative_line, limit=MAX_PUBLIC_LINE_CHARS)
        state_key = self._state_key(service, incident, raw_logs, now)
        record = self.state.record_seen(
            fingerprint=state_key,
            service_name=service.name,
            issue_repo=service.issue_repo,
            now=now,
        )

        title = issue_title(service, incident, state_key)
        labels = sorted(set(service.labels + ("homelab-sre", incident.severity.lower())))
        if record.issue_number is None:
            issue = self.github.create_issue(
                repo=service.issue_repo,
                title=title,
                body=issue_body(service, incident, analysis, sanitized_line, bundle, self.config.dry_run),
                labels=labels,
            )
            self.state.set_issue(fingerprint=state_key, issue_number=issue.number, issue_url=issue.url)
            issue_result = issue
            issue_action = "created"
        else:
            issue_result = IssueResult(
                repo=service.issue_repo,
                number=record.issue_number,
                url=record.issue_url or f"https://github.com/{service.issue_repo}/issues/{record.issue_number}",
            )
            self.github.comment_issue(
                repo=service.issue_repo,
                issue_number=record.issue_number,
                body=issue_comment(incident, sanitized_line, bundle),
            )
            issue_action = "updated"

        dispatch = self._maybe_dispatch_codex(service, incident, issue_result, state_key, now)
        LOGGER.info("%s issue for %s fingerprint=%s", issue_action, service.name, incident.fingerprint[:12])
        return {
            "ok": True,
            "status": issue_action,
            "service": service.name,
            "issue": {"repo": issue_result.repo, "number": issue_result.number, "url": issue_result.url},
            "dispatch": dispatch,
            "diagnostic": {"path": bundle.path, "reference": bundle.reference},
            "dry_run": self.config.dry_run,
        }

    def _collect_logs(self, service: ServiceMetadata, incident: Incident, now: datetime) -> str:
        try:
            return self.logs.collect_many(
                container_names=service.containers,
                incident_container_id=incident.container_id,
                incident_container_name=incident.container_name,
                tail=self.config.docker_log_tail,
                since=now - timedelta(seconds=self.config.docker_log_lookback_seconds),
            )
        except Exception as exc:
            LOGGER.warning("Could not collect Docker logs for %s: %s", incident.container_name, exc)
            return f"Could not collect Docker logs: {exc}"

    def _write_diagnostic_bundle(
        self,
        service: ServiceMetadata,
        incident: Incident,
        raw_logs: str,
        now: datetime,
    ) -> DiagnosticBundle:
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        target_dir = self.config.diagnostic_dir / safe_segment(service.name)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{stamp}-{incident.fingerprint[:12]}.log"
        text = "\n".join(
            [
                f"service={service.name}",
                f"container={incident.container_name}",
                f"image={incident.image}",
                f"severity={incident.severity}",
                f"fingerprint={incident.fingerprint}",
                f"occurred_at={incident.occurred_at}",
                f"detected_at={incident.detected_at}",
                f"log_lookback_seconds={self.config.docker_log_lookback_seconds}",
                "",
                raw_logs,
            ]
        )
        path.write_text(limit_bytes(text, self.config.diagnostic_max_bytes), encoding="utf-8")
        return DiagnosticBundle(path=str(path), reference=self._diagnostic_reference(path))

    def _diagnostic_reference(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.config.diagnostic_dir)
        except ValueError:
            return str(path)
        return f"{self.config.diagnostic_reference_root.rstrip('/')}/{relative}"

    def _state_key(self, service: ServiceMetadata, incident: Incident, raw_logs: str, now: datetime) -> str:
        if self.config.episode_window_seconds > 0:
            recent = self.state.recent_incident_for_service(
                service_name=service.name,
                issue_repo=service.issue_repo,
                since=now - timedelta(seconds=self.config.episode_window_seconds),
            )
            if recent is not None and recent.issue_number is not None:
                return recent.fingerprint

        analysis = analyze_incident(incident, raw_logs)
        signature = "|".join(
            [
                service.name,
                incident.container_name,
                incident.severity,
                normalize_for_key(analysis.representative_line),
            ]
        )
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        return f"episode:{service.name}:{digest}"

    def _maybe_dispatch_codex(
        self,
        service: ServiceMetadata,
        incident: Incident,
        issue: IssueResult,
        state_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        if service.unknown:
            return {"attempted": False, "reason": "unknown service"}
        if not service.autofix:
            return {"attempted": False, "reason": "autofix disabled"}
        if not service.source_repo:
            return {"attempted": False, "reason": "source repo missing"}
        cooldown_since = now - timedelta(seconds=self.config.investigation_cooldown_seconds)
        if self.state.recent_dispatch_exists(fingerprint=state_key, since=cooldown_since):
            return {"attempted": False, "reason": "investigation cooldown"}
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.state.dispatch_count(source_repo=service.source_repo, since=day_start) >= service.repo_daily_limit:
            return {"attempted": False, "reason": "repo daily limit"}
        if self.state.dispatch_count(since=day_start) >= self.config.codex_global_daily_limit:
            return {"attempted": False, "reason": "global daily limit"}

        payload = {
            "issue_number": issue.number,
            "issue_url": issue.url,
            "fingerprint": incident.fingerprint,
            "service_name": service.name,
            "container_name": incident.container_name,
            "deployment_repo": service.deploy_repo,
            "deployment_path": service.deploy_path,
        }
        self.github.repository_dispatch(
            repo=service.source_repo,
            event_type="homelab-sre-investigate",
            client_payload=payload,
        )
        self.state.record_dispatch(
            source_repo=service.source_repo,
            service_name=service.name,
            fingerprint=state_key,
            now=now,
        )
        return {"attempted": True, "repo": service.source_repo, "event_type": "homelab-sre-investigate"}

    def _catalog(self) -> ServiceCatalog:
        if self.catalog_loader is not None:
            return self.catalog_loader()
        assert self.catalog is not None
        return self.catalog


def issue_title(service: ServiceMetadata, incident: Incident, state_key: str) -> str:
    digest = hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:12]
    return f"[homelab-sre] {service.name} {incident.severity} {digest}"


def issue_body(
    service: ServiceMetadata,
    incident: Incident,
    analysis: IssueAnalysis,
    sanitized_line: str,
    bundle: DiagnosticBundle,
    dry_run: bool,
) -> str:
    return "\n".join(
        [
            "## Summary",
            "",
            analysis.summary,
            "",
            "## Observed",
            "",
            f"- {redact_text(analysis.observed, limit=MAX_PUBLIC_LINE_CHARS)}",
            f"- Relevant redacted log line: `{sanitized_line}`",
            f"- Full local diagnostic bundle: `{bundle.reference}`",
            "",
            "## Expected",
            "",
            f"- {analysis.expected}",
            "",
            "## Incident",
            "",
            f"- Fingerprint: `{incident.fingerprint}`",
            f"- Occurred at: `{incident.occurred_at}`",
            f"- Detected at: `{incident.detected_at}`",
            f"- Image: `{incident.image}`",
            f"- Matched pattern: `{incident.matched_pattern}`",
            "",
            "## Deployment Metadata",
            "",
            f"- Service: `{service.name}`",
            f"- Source repo: `{service.source_repo or 'unknown'}`",
            f"- Source path hint: `{service.source_path_hint or 'unknown'}`",
            f"- Deploy repo: `{service.deploy_repo}`",
            f"- Deploy path: `{service.deploy_path or 'unknown'}`",
            f"- Runbook: {service.runbook_url or 'none'}",
            f"- Codex autofix: `{service.autofix}`",
            f"- SRE dry run: `{dry_run}`",
            "",
            "## Expected Fix Discipline",
            "",
            "Start from this incident summary and use targeted code search. Do not scan the whole repo unless the evidence requires it.",
            "Keep fixes narrowly scoped to the failure. Prefer a small PR with tests or a clear validation note.",
        ]
    )


def issue_comment(incident: Incident, sanitized_line: str, bundle: DiagnosticBundle) -> str:
    return "\n".join(
        [
            "The same incident episode was observed again.",
            "",
            f"- Occurred at: `{incident.occurred_at}`",
            f"- Detected at: `{incident.detected_at}`",
            f"- Relevant redacted log line: `{sanitized_line}`",
            f"- Full local diagnostic bundle: `{bundle.reference}`",
        ]
    )


def analyze_incident(incident: Incident, raw_logs: str) -> IssueAnalysis:
    representative = representative_log_line(incident, raw_logs)
    clean = strip_log_timestamp(representative)
    exception = EXCEPTION_RE.search(clean)
    if exception:
        exception_type = exception.group(1)
        exception_message = exception.group(2).strip()
        return IssueAnalysis(
            summary=f"`{incident.container_name}` hit `{exception_type}` near a `{incident.severity}` log event.",
            observed=f"The service raised `{exception_type}`: {exception_message}",
            expected="The service should handle the request without raising an exception or emitting an error log.",
            representative_line=representative,
        )
    return IssueAnalysis(
        summary=f"`{incident.container_name}` emitted a `{incident.severity}` log event matched by `{incident.matched_pattern}`.",
        observed=f"The service logged: {clean}",
        expected="The service should run without unexpected ERROR/WARN log events.",
        representative_line=representative,
    )


def representative_log_line(incident: Incident, raw_logs: str) -> str:
    for line in reversed(raw_logs.splitlines()):
        if EXCEPTION_RE.search(line):
            return line.strip()
    for line in raw_logs.splitlines():
        if incident.severity and incident.severity.upper() in line.upper():
            return line.strip()
    return incident.line.strip() or incident.normalized_line.strip() or "No representative log line captured."


def strip_log_timestamp(line: str) -> str:
    parts = line.split(maxsplit=1)
    if len(parts) == 2 and "T" in parts[0] and ":" in parts[0]:
        return parts[1].strip()
    return line.strip()


def normalize_for_key(value: str) -> str:
    return re.sub(r"\s+", " ", strip_log_timestamp(value)).strip().lower()


def limit_bytes(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = encoded[-max_bytes:].decode("utf-8", errors="replace")
    return f"<truncated to last {max_bytes} bytes>\n{suffix}"


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned or "unknown"


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
