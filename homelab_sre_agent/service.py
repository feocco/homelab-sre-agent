from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Callable

from .config import Config
from .docker_logs import DockerLogCollector
from .github import GitHubClient, IssueResult
from .metadata import ServiceCatalog, ServiceMetadata
from .redact import redact_text
from .state import StateStore, utc_now


LOGGER = logging.getLogger("homelab-sre-agent")
MAX_ISSUE_LOG_CHARS = 8000
MAX_COMMENT_LOG_CHARS = 4000


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

        raw_logs = self._collect_logs(incident)
        sanitized_line = redact_text(incident.line, limit=2000)
        sanitized_logs = redact_text(raw_logs, limit=MAX_ISSUE_LOG_CHARS)
        record = self.state.record_seen(
            fingerprint=incident.fingerprint,
            service_name=service.name,
            issue_repo=service.issue_repo,
            now=now,
        )

        title = issue_title(service, incident)
        labels = sorted(set(service.labels + ("homelab-sre", incident.severity.lower())))
        if record.issue_number is None:
            issue = self.github.create_issue(
                repo=service.issue_repo,
                title=title,
                body=issue_body(service, incident, sanitized_line, sanitized_logs, self.config.dry_run),
                labels=labels,
            )
            self.state.set_issue(fingerprint=incident.fingerprint, issue_number=issue.number, issue_url=issue.url)
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
                body=issue_comment(incident, sanitized_line, redact_text(raw_logs, limit=MAX_COMMENT_LOG_CHARS)),
            )
            issue_action = "updated"

        dispatch = self._maybe_dispatch_codex(service, incident, issue_result, now)
        LOGGER.info("%s issue for %s fingerprint=%s", issue_action, service.name, incident.fingerprint[:12])
        return {
            "ok": True,
            "status": issue_action,
            "service": service.name,
            "issue": {"repo": issue_result.repo, "number": issue_result.number, "url": issue_result.url},
            "dispatch": dispatch,
            "dry_run": self.config.dry_run,
        }

    def _collect_logs(self, incident: Incident) -> str:
        try:
            return self.logs.collect(
                container_id=incident.container_id,
                container_name=incident.container_name,
                tail=self.config.docker_log_tail,
            )
        except Exception as exc:
            LOGGER.warning("Could not collect Docker logs for %s: %s", incident.container_name, exc)
            return f"Could not collect Docker logs: {exc}"

    def _maybe_dispatch_codex(
        self,
        service: ServiceMetadata,
        incident: Incident,
        issue: IssueResult,
        now,
    ) -> dict[str, Any]:
        if service.unknown:
            return {"attempted": False, "reason": "unknown service"}
        if not service.autofix:
            return {"attempted": False, "reason": "autofix disabled"}
        if not service.source_repo:
            return {"attempted": False, "reason": "source repo missing"}
        cooldown_since = now - timedelta(seconds=self.config.investigation_cooldown_seconds)
        if self.state.recent_dispatch_exists(fingerprint=incident.fingerprint, since=cooldown_since):
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
            fingerprint=incident.fingerprint,
            now=now,
        )
        return {"attempted": True, "repo": service.source_repo, "event_type": "homelab-sre-investigate"}

    def _catalog(self) -> ServiceCatalog:
        if self.catalog_loader is not None:
            return self.catalog_loader()
        assert self.catalog is not None
        return self.catalog


def issue_title(service: ServiceMetadata, incident: Incident) -> str:
    return f"[homelab-sre] {service.name} {incident.severity} {incident.fingerprint[:12]}"


def issue_body(
    service: ServiceMetadata,
    incident: Incident,
    sanitized_line: str,
    sanitized_logs: str,
    dry_run: bool,
) -> str:
    return "\n".join(
        [
            "## Summary",
            "",
            f"`{incident.container_name}` emitted a `{incident.severity}` log line matched by `{incident.matched_pattern}`.",
            "",
            "## Incident",
            "",
            f"- Fingerprint: `{incident.fingerprint}`",
            f"- Occurred at: `{incident.occurred_at}`",
            f"- Detected at: `{incident.detected_at}`",
            f"- Image: `{incident.image}`",
            f"- Message: `{sanitized_line}`",
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
            "## Recent Logs",
            "",
            "```text",
            sanitized_logs,
            "```",
            "",
            "## Expected Fix Discipline",
            "",
            "Keep fixes narrowly scoped to the failure. Prefer a small PR with tests or a clear validation note.",
        ]
    )


def issue_comment(incident: Incident, sanitized_line: str, sanitized_logs: str) -> str:
    return "\n".join(
        [
            "The same fingerprint was observed again.",
            "",
            f"- Occurred at: `{incident.occurred_at}`",
            f"- Detected at: `{incident.detected_at}`",
            f"- Message: `{sanitized_line}`",
            "",
            "```text",
            sanitized_logs,
            "```",
        ]
    )


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
