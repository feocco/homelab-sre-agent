from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatch
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Config
from .docker_logs import DockerLogCollector
from .github import GitHubClient, GitHubIssue, IssueResult
from .metadata import ServiceCatalog, ServiceMetadata
from .notifications import IssueNotifier, PHONE_APPROVAL_TOKEN_TTL_SECONDS, make_approval_action
from .redact import redact_text
from .state import CommentRollup, RecurrenceSummary, StateStore, format_dt, parse_dt, utc_now


LOGGER = logging.getLogger("homelab-sre-agent")
MAX_PUBLIC_LINE_CHARS = 1000
ISSUE_CREATE_CLAIM_TTL_SECONDS = 300
EXCEPTION_RE = re.compile(r"\b([A-Za-z_][\w.]*Error|Exception):\s+(.+)")
AUTOFIX_PENDING_LABEL = "sre:autofix-pending"
AUTOFIX_APPROVED_LABEL = "sre:autofix-approved"
AUTOFIX_STARTED_LABEL = "sre:autofix-started"
AUTOFIX_BLOCKED_LABEL = "sre:autofix-blocked"
HUMAN_INVESTIGATING_LABEL = "sre:human-investigating"
SRE_LABEL = "homelab-sre"
AGENT_COMMENT_HEADER = "**homelab-sre-agent**"
LEGACY_AUTOFIX_STATUS_LABELS = (AUTOFIX_PENDING_LABEL, AUTOFIX_STARTED_LABEL, AUTOFIX_BLOCKED_LABEL)
AUTOFIX_LABELS = {
    AUTOFIX_APPROVED_LABEL: ("bbf7d0", "Approval for the homelab SRE agent to dispatch Codex."),
    HUMAN_INVESTIGATING_LABEL: ("ddd6fe", "A human is investigating; SRE autofix should not start."),
}


class DiagnosticPublisher(Protocol):
    def upload(
        self,
        *,
        service: ServiceMetadata,
        issue: IssueResult,
        state_key: str,
        diagnostic_reference: str,
        recurrence: RecurrenceSummary,
        now: datetime,
    ) -> str: ...

    def presign(self, object_key: str) -> str: ...


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


@dataclass(frozen=True)
class OperationalRoute:
    fingerprint: str
    dependency: str
    reason: str


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
        issue_notifier: IssueNotifier | None = None,
        now_func: Callable[[], datetime] | None = None,
        diagnostic_publisher: DiagnosticPublisher | None = None,
    ) -> None:
        self.config = config
        if catalog is None and catalog_loader is None:
            raise ValueError("catalog or catalog_loader is required")
        self.catalog = catalog
        self.catalog_loader = catalog_loader
        self.state = state
        self.github = github
        self.logs = logs
        self.issue_notifier = issue_notifier
        self.now_func = now_func or utc_now
        self.diagnostic_publisher = diagnostic_publisher

    def handle_incident(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now_func()
        incident = Incident.from_payload(payload)
        service = self._catalog().match(container_name=incident.container_name, image=incident.image)

        if not service.sre_enabled:
            return {"ok": True, "status": "ignored", "reason": "sre disabled", "service": service.name}

        raw_logs = self._collect_logs(service, incident, now)
        bundle = self._write_diagnostic_bundle(service, incident, raw_logs, now)
        analysis = analyze_incident(incident, raw_logs)
        sanitized_line = redact_text(analysis.representative_line, limit=MAX_PUBLIC_LINE_CHARS)
        operational_route = classify_operational_route(service, analysis)
        if operational_route is not None:
            return self._handle_operational_incident(
                service=service,
                route=operational_route,
                line=sanitized_line,
                bundle=bundle,
                now=now,
            )
        state_key = self._state_key(service, incident, raw_logs, now)
        claim = self.state.claim_issue_for_incident(
            fingerprint=state_key,
            service_name=service.name,
            issue_repo=service.issue_repo,
            now=now,
            claim_ttl_seconds=ISSUE_CREATE_CLAIM_TTL_SECONDS,
        )
        recurrence = self.state.record_observation(
            fingerprint=state_key,
            service_name=service.name,
            issue_repo=service.issue_repo,
            source_fingerprint=incident.fingerprint,
            severity=incident.severity,
            observed_at=now,
            representative_line=sanitized_line,
            diagnostic_reference=bundle.reference,
        )

        title = issue_title(service, incident, state_key)
        labels = sorted(set(service.labels + (SRE_LABEL, incident.severity.lower())))
        if claim.action == "create_issue":
            self._ensure_service_labels(service, labels)
            issue = self.github.find_open_issue_by_title(repo=service.issue_repo, title=title, label=SRE_LABEL)
            issue_action = "reused"
            if issue is None:
                try:
                    issue = self.github.create_issue(
                        repo=service.issue_repo,
                        title=title,
                        body=issue_body(
                            service,
                            incident,
                            analysis,
                            sanitized_line,
                            bundle,
                            self.config.dry_run,
                            state_key,
                            recurrence,
                        ),
                        labels=labels,
                    )
                except Exception:
                    self.state.fail_issue_claim(fingerprint=state_key, now=now)
                    raise
                issue_action = "created"
            issue_result = issue
            self.state.set_issue(fingerprint=state_key, issue_number=issue.number, issue_url=issue.url, now=now)
            self._upload_diagnostic_summary(
                service=service,
                issue=issue_result,
                state_key=state_key,
                diagnostic_reference=bundle.reference,
                recurrence=recurrence,
                now=now,
            )
            if issue_action == "created":
                self._notify_issue_created(service, incident, analysis, sanitized_line, state_key, issue, now)
        elif claim.action == "issue_creation_in_flight":
            dispatch = self._autofix_wait_status(service)
            LOGGER.info("Deferred issue update for %s fingerprint=%s", service.name, incident.fingerprint[:12])
            return {
                "ok": True,
                "status": "issue_creation_in_flight",
                "service": service.name,
                "issue": None,
                "dispatch": dispatch,
                "diagnostic": {"path": bundle.path, "reference": bundle.reference},
                "dry_run": self.config.dry_run,
            }
        else:
            issue_result = IssueResult(
                repo=service.issue_repo,
                number=claim.record.issue_number or 0,
                url=claim.record.issue_url or f"https://github.com/{service.issue_repo}/issues/{claim.record.issue_number}",
            )
            rollup = self.state.record_pending_comment(
                fingerprint=state_key,
                now=now,
                line=sanitized_line,
                bundle_reference=bundle.reference,
                cooldown_seconds=self.config.issue_comment_cooldown_seconds,
            )
            if rollup is not None and claim.record.issue_number is not None:
                self.github.comment_issue(
                    repo=service.issue_repo,
                    issue_number=claim.record.issue_number,
                    body=agent_comment(issue_comment(rollup, recurrence)),
                )
                self.state.mark_comment_sent(fingerprint=state_key, now=now)
            issue_action = "updated"
            if issue_result.number:
                self._upload_diagnostic_summary(
                    service=service,
                    issue=issue_result,
                    state_key=state_key,
                    diagnostic_reference=bundle.reference,
                    recurrence=recurrence,
                    now=now,
                )

        dispatch = self._autofix_wait_status(service)
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

    def _upload_diagnostic_summary(
        self,
        *,
        service: ServiceMetadata,
        issue: IssueResult,
        state_key: str,
        diagnostic_reference: str,
        recurrence: RecurrenceSummary,
        now: datetime,
    ) -> None:
        if self.diagnostic_publisher is None:
            return
        try:
            object_key = self.diagnostic_publisher.upload(
                service=service,
                issue=issue,
                state_key=state_key,
                diagnostic_reference=diagnostic_reference,
                recurrence=recurrence,
                now=now,
            )
        except Exception:
            LOGGER.exception("Could not upload diagnostic context for %s", state_key)
            return
        self.state.set_diagnostic_object_key(fingerprint=state_key, object_key=object_key, uploaded_at=now)

    def _handle_operational_incident(
        self,
        *,
        service: ServiceMetadata,
        route: OperationalRoute,
        line: str,
        bundle: DiagnosticBundle,
        now: datetime,
    ) -> dict[str, Any]:
        record = self.state.record_operational_observation(
            fingerprint=route.fingerprint,
            service_name=service.name,
            issue_repo=service.issue_repo,
            dependency=route.dependency,
            reason=route.reason,
            observed_at=now,
            line=line,
            diagnostic_reference=bundle.reference,
        )
        notification_sent = False
        notification_reason = "disabled"
        if self.config.issue_notifications_enabled and self.issue_notifier is not None:
            last_notification_at = parse_dt(record.last_notification_at)
            if (
                last_notification_at is None
                or now - last_notification_at >= timedelta(seconds=self.config.operational_notification_cooldown_seconds)
            ):
                try:
                    self.issue_notifier.send_operational_incident(
                        service=service,
                        dependency=record.dependency,
                        reason=record.reason,
                        line=record.latest_line,
                        total_count=record.total_count,
                        latest_seen_at=record.latest_seen_at,
                    )
                except Exception:
                    LOGGER.exception("Failed to send SRE operational notification")
                    notification_reason = "send_failed"
                else:
                    record = self.state.mark_operational_notification_sent(fingerprint=route.fingerprint, now=now)
                    notification_sent = True
                    notification_reason = "sent"
            else:
                notification_reason = "cooldown"

        LOGGER.info(
            "Routed operational incident for %s dependency=%s fingerprint=%s",
            service.name,
            route.dependency,
            route.fingerprint[:12],
        )
        return {
            "ok": True,
            "status": "operational_dependency",
            "service": service.name,
            "issue": None,
            "route": {
                "classification": "operational_dependency",
                "fingerprint": route.fingerprint,
                "dependency": route.dependency,
                "reason": route.reason,
            },
            "notification": {"sent": notification_sent, "reason": notification_reason},
            "diagnostic": {"path": bundle.path, "reference": bundle.reference},
            "dry_run": self.config.dry_run,
        }

    def poll_autofix_approvals(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        catalog = self._catalog()
        repos = self._autofix_issue_repos(catalog)
        results: list[dict[str, Any]] = []

        for repo in repos:
            issues = self.github.list_open_issues_with_label(repo=repo, label=AUTOFIX_APPROVED_LABEL)
            for issue in issues:
                results.append(self._handle_autofix_approval(catalog, issue, now))

        if results:
            LOGGER.info("Processed %s approved autofix issue(s)", len(results))
        return {"ok": True, "checked_repos": repos, "processed": len(results), "results": results}

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
        clean_line = strip_log_timestamp(analysis.representative_line)
        exception = EXCEPTION_RE.search(clean_line)
        if exception:
            signature = "|".join(
                [
                    service.name,
                    incident.container_name,
                    "exception",
                    exception.group(1),
                    normalize_for_key(exception.group(2)),
                ]
            )
        else:
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

    def _dispatch_codex_for_issue(
        self,
        service: ServiceMetadata,
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

        fingerprint = dispatch_fingerprint(state_key)
        branch = f"codex/sre-{issue.number}-{fingerprint}"
        if self.github.open_pull_request_exists(repo=service.source_repo, branch=branch):
            return {"attempted": False, "reason": "open SRE PR", "branch": branch}

        payload = {
            "issue_number": issue.number,
            "issue_url": issue.url,
            "fingerprint": fingerprint,
            "state_fingerprint": state_key,
            "service_name": service.name,
            "container_name": service.containers[0] if service.containers else service.name,
            "deployment_repo": service.deploy_repo,
            "deployment_path": service.deploy_path,
        }
        record = self.state.get_incident(state_key)
        if self.diagnostic_publisher is not None and record is not None:
            reference = record.latest_diagnostic_reference or ""
            object_key = record.latest_diagnostic_object_key or ""
            if reference and object_key:
                try:
                    payload["diagnostic_url"] = self.diagnostic_publisher.presign(object_key)
                    payload["diagnostic_reference"] = reference
                    payload.update(self._diagnostic_retention_payload(record))
                except Exception:
                    LOGGER.exception("Could not publish diagnostic context for %s", state_key)
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
        return {
            "attempted": True,
            "repo": service.source_repo,
            "event_type": "homelab-sre-investigate",
            "branch": branch,
        }

    def _diagnostic_retention_payload(self, record) -> dict[str, str]:
        uploaded_at = parse_dt(record.latest_diagnostic_uploaded_at)
        if uploaded_at is None:
            return {}
        payload = {"diagnostic_uploaded_at": format_dt(uploaded_at)}
        if self.config.diagnostic_retention_days > 0:
            expires_at = uploaded_at + timedelta(days=self.config.diagnostic_retention_days)
            payload["diagnostic_expires_at"] = format_dt(expires_at)
        return payload

    def ensure_autofix_labels(self) -> None:
        self._ensure_autofix_labels(self._autofix_issue_repos(self._catalog()))

    def approve_autofix_from_phone(self, token: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        approval = self.state.consume_phone_approval_token(
            token,
            now=now,
            max_age_seconds=PHONE_APPROVAL_TOKEN_TTL_SECONDS,
        )
        if approval is None:
            return {"ok": True, "status": "ignored", "reason": "unknown stale or used token"}

        self._ensure_labels(approval.issue_repo, (AUTOFIX_APPROVED_LABEL,))
        self.github.add_issue_labels(
            repo=approval.issue_repo,
            issue_number=approval.issue_number,
            labels=[AUTOFIX_APPROVED_LABEL],
        )
        self.github.comment_issue(
            repo=approval.issue_repo,
            issue_number=approval.issue_number,
            body=agent_comment(
                "Autofix approved from the phone notification. The SRE agent will run normal safety gates before dispatching Codex."
            ),
        )
        return {
            "ok": True,
            "status": "approved",
            "issue": {
                "repo": approval.issue_repo,
                "number": approval.issue_number,
                "url": approval.issue_url,
            },
        }

    def _handle_autofix_approval(
        self,
        catalog: ServiceCatalog,
        issue: GitHubIssue,
        now: datetime,
    ) -> dict[str, Any]:
        labels = set(issue.labels)
        if SRE_LABEL not in labels:
            return {"issue": issue.number, "status": "ignored", "reason": "missing homelab-sre label"}
        if HUMAN_INVESTIGATING_LABEL in labels:
            return {"issue": issue.number, "status": "skipped", "reason": "human investigating"}

        record = self.state.get_incident_by_issue(issue_repo=issue.repo, issue_number=issue.number)
        if record is None:
            return self._block_autofix(
                issue,
                reason="no matching incident state",
                comment=self._autofix_block_comment(reason="no matching incident state"),
            )

        service = next(
            (item for item in catalog.services if item.name == record.service_name and item.issue_repo == record.issue_repo),
            None,
        )
        if service is None:
            return self._block_autofix(
                issue,
                reason="service metadata missing",
                comment=self._autofix_block_comment(
                    reason="service metadata missing",
                    service_name=record.service_name,
                ),
            )

        issue_result = IssueResult(repo=issue.repo, number=issue.number, url=issue.url)
        try:
            dispatch = self._dispatch_codex_for_issue(service, issue_result, record.fingerprint, now)
        except Exception as exc:
            LOGGER.exception("Autofix dispatch failed for %s#%s", issue.repo, issue.number)
            return self._block_autofix(
                issue,
                reason="dispatch error",
                comment=self._autofix_block_comment(
                    reason="dispatch error",
                    service_name=service.name,
                    source_repo=service.source_repo,
                    detail=redact_text(str(exc), limit=MAX_PUBLIC_LINE_CHARS),
                ),
            )
        if dispatch.get("attempted"):
            self._mark_autofix_started(issue, dispatch)
            return {"issue": issue.number, "status": "dispatched", "dispatch": dispatch}

        reason = str(dispatch.get("reason") or "dispatch blocked")
        if reason in {"investigation cooldown", "open SRE PR"}:
            self._mark_autofix_started(issue, dispatch)
            return {"issue": issue.number, "status": "already-started", "dispatch": dispatch}

        return self._block_autofix(
            issue,
            reason=reason,
            comment=self._autofix_block_comment(
                reason=reason,
                service_name=service.name,
                source_repo=service.source_repo,
                repo_daily_limit=service.repo_daily_limit,
            ),
        )

    def _mark_autofix_started(self, issue: GitHubIssue, dispatch: dict[str, Any]) -> None:
        self._remove_autofix_control_labels(issue)
        self.github.comment_issue(repo=issue.repo, issue_number=issue.number, body=self._autofix_dispatch_comment(dispatch))

    def _block_autofix(self, issue: GitHubIssue, *, reason: str, comment: str) -> dict[str, Any]:
        self._remove_autofix_control_labels(issue)
        self.github.comment_issue(repo=issue.repo, issue_number=issue.number, body=comment)
        return {"issue": issue.number, "status": "blocked", "reason": reason}

    def _remove_autofix_control_labels(self, issue: GitHubIssue) -> None:
        self.github.remove_issue_label(repo=issue.repo, issue_number=issue.number, label=AUTOFIX_APPROVED_LABEL)
        for label in LEGACY_AUTOFIX_STATUS_LABELS:
            self.github.remove_issue_label(repo=issue.repo, issue_number=issue.number, label=label)

    def _autofix_dispatch_comment(self, dispatch: dict[str, Any]) -> str:
        branch = dispatch.get("branch")
        reason = str(dispatch.get("reason") or "")
        if dispatch.get("attempted"):
            lines = [
                "Autofix approval accepted.",
                "",
                "- Result: sent `repository_dispatch` to the source repo.",
                f"- Source repo: `{dispatch.get('repo')}`",
                f"- Event type: `{dispatch.get('event_type')}`",
            ]
            if branch:
                lines.append(f"- Expected branch: `{branch}`")
            return agent_comment("\n".join(lines))

        if reason == "open SRE PR":
            lines = [
                "Autofix approval accepted, but no new Codex workflow was dispatched.",
                "",
                "- Reason: an open SRE pull request already exists for this issue.",
            ]
            if branch:
                lines.append(f"- Existing branch: `{branch}`")
            return agent_comment("\n".join(lines))

        if reason == "investigation cooldown":
            return agent_comment(
                "\n".join(
                    [
                        "Autofix approval accepted, but no new Codex workflow was dispatched.",
                        "",
                        "- Reason: this issue is still inside the investigation cooldown for this incident fingerprint.",
                        f"- Cooldown: `{self.config.investigation_cooldown_seconds}` seconds.",
                    ]
                )
            )

        return agent_comment(
            "\n".join(
                [
                    "Autofix approval accepted, but no new Codex workflow was dispatched.",
                    "",
                    f"- Reason: `{reason or 'unknown'}`",
                ]
            )
        )

    def _autofix_block_comment(
        self,
        *,
        reason: str,
        service_name: str | None = None,
        source_repo: str | None = None,
        repo_daily_limit: int | None = None,
        detail: str | None = None,
    ) -> str:
        lines = [
            "Autofix approval was not dispatched.",
            "",
            f"- Reason: `{reason}`",
        ]
        if service_name:
            lines.append(f"- Service: `{service_name}`")
        if source_repo:
            lines.append(f"- Source repo: `{source_repo}`")

        if reason == "global daily limit":
            lines.extend(
                [
                    "- What happened: the SRE agent already reached its global Codex dispatch cap for today.",
                    f"- Current limit: `{self.config.codex_global_daily_limit}` dispatch(es) per UTC day via `SRE_CODEX_GLOBAL_DAILY_LIMIT`.",
                    "- Retry: wait until the next UTC day, or raise the global limit and apply `sre:autofix-approved` again.",
                ]
            )
        elif reason == "repo daily limit":
            limit = repo_daily_limit if repo_daily_limit is not None else "unknown"
            lines.extend(
                [
                    "- What happened: this source repo already reached its per-repo Codex dispatch cap for today.",
                    f"- Current limit: `{limit}` dispatch(es) per UTC day from service metadata.",
                    "- Retry: wait until the next UTC day, or raise the service `repo_daily_limit` and apply `sre:autofix-approved` again.",
                ]
            )
        elif reason == "autofix disabled":
            lines.append("- What happened: service metadata does not opt this service into Codex autofix.")
        elif reason == "source repo missing":
            lines.append("- What happened: service metadata does not include a source repo to dispatch the workflow into.")
        elif reason == "unknown service":
            lines.append("- What happened: the incident did not match an enabled service metadata entry.")
        elif reason == "no matching incident state":
            lines.extend(
                [
                    "- What happened: the GitHub issue could not be matched to local SRE SQLite state on the NAS.",
                    "- Retry: wait for a fresh incident update, or recreate the issue through the SRE incident flow.",
                ]
            )
        elif reason == "service metadata missing":
            lines.append("- What happened: the issue maps to a service that is no longer present in SRE metadata.")
        elif reason == "dispatch error":
            lines.append("- What happened: the agent attempted to dispatch the GitHub workflow and the call failed.")
        else:
            lines.append("- What happened: a safety gate prevented the Codex workflow from starting.")

        if detail:
            lines.append(f"- Error detail: `{detail}`")
        lines.append("- Result: no Codex workflow was triggered.")
        return agent_comment("\n".join(lines))

    def _autofix_wait_status(self, service: ServiceMetadata) -> dict[str, Any]:
        if service.unknown:
            return {"attempted": False, "reason": "unknown service"}
        if not service.autofix:
            return {"attempted": False, "reason": "autofix disabled"}
        if not service.source_repo:
            return {"attempted": False, "reason": "source repo missing"}
        return {
            "attempted": False,
            "reason": "autofix approval required",
            "approval_label": AUTOFIX_APPROVED_LABEL,
        }

    def _autofix_eligible(self, service: ServiceMetadata) -> bool:
        return not service.unknown and service.autofix and bool(service.source_repo)

    def _ensure_service_labels(self, service: ServiceMetadata, labels: tuple[str, ...] | list[str]) -> None:
        self._ensure_labels(service.issue_repo, labels)
        if self._autofix_eligible(service):
            self._ensure_autofix_labels([service.issue_repo])

    def _notify_issue_created(
        self,
        service: ServiceMetadata,
        incident: Incident,
        analysis: IssueAnalysis,
        sanitized_line: str,
        state_key: str,
        issue: IssueResult,
        now: datetime,
    ) -> None:
        if not self.config.issue_notifications_enabled or self.issue_notifier is None:
            return

        approval_action = None
        if self.config.phone_approvals_enabled and self._autofix_eligible(service):
            token = self.state.create_phone_approval_token(
                fingerprint=state_key,
                service_name=service.name,
                issue_repo=issue.repo,
                issue_number=issue.number,
                issue_url=issue.url,
                now=now,
            )
            approval_action = make_approval_action(token)

        try:
            self.issue_notifier.send_issue_created(
                service=service,
                incident=incident,
                analysis=analysis,
                issue=issue,
                sanitized_line=sanitized_line,
                approval_action=approval_action,
            )
        except Exception:
            LOGGER.exception("Failed to send SRE issue notification")

    def _autofix_issue_repos(self, catalog: ServiceCatalog) -> list[str]:
        return sorted({service.issue_repo for service in catalog.services if service.sre_enabled and service.autofix})

    def _ensure_autofix_labels(self, repos: list[str]) -> None:
        for repo in repos:
            self._ensure_labels(repo, tuple(AUTOFIX_LABELS))

    def _ensure_labels(self, repo: str, labels: tuple[str, ...] | list[str]) -> None:
        for label in labels:
            color, description = AUTOFIX_LABELS.get(label, ("ededed", "Homelab SRE"))
            self.github.ensure_label(repo=repo, name=label, color=color, description=description)

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
    state_key: str,
    recurrence: RecurrenceSummary,
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
            f"- SRE state fingerprint: `{state_key}`",
            f"- Fingerprint: `{incident.fingerprint}`",
            f"- Occurred at: `{incident.occurred_at}`",
            f"- Detected at: `{incident.detected_at}`",
            f"- Image: `{incident.image}`",
            f"- Matched pattern: `{incident.matched_pattern}`",
            "",
            "## Recurrence",
            "",
            recurrence_summary_lines(recurrence),
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
            f"- Autofix approval label: `{AUTOFIX_APPROVED_LABEL}`" if service.autofix else "- Autofix approval label: `not enabled`",
            f"- SRE dry run: `{dry_run}`",
            "",
            "## Expected Fix Discipline",
            "",
            "Start from this incident summary and use targeted code search. Do not scan the whole repo unless the evidence requires it.",
            "Keep fixes narrowly scoped to the failure. Prefer a small PR with tests or a clear validation note.",
            "State assumptions when they affect behavior, avoid speculative changes, and explain tradeoffs when choosing suppression over a fix.",
            "If downgrading or suppressing a connection error, identify the retry/backoff path and why the event is transient enough for WARN/INFO.",
        ]
    )


def issue_comment(rollup: CommentRollup, recurrence: RecurrenceSummary) -> str:
    return "\n".join(
        [
            f"Observed {rollup.count} more times since the last update.",
            "",
            f"- First repeated observation: `{rollup.first_seen_at}`",
            f"- Latest repeated observation: `{rollup.last_seen_at}`",
            f"- Latest redacted log line: `{rollup.line}`",
            f"- Latest local diagnostic bundle: `{rollup.bundle_reference}`",
            f"- Likelihood next 14 days: `{recurrence.likelihood}`",
        ]
    )


def agent_comment(body: str) -> str:
    return f"{AGENT_COMMENT_HEADER}\n\n{body}"


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


def classify_operational_route(
    service: ServiceMetadata,
    analysis: IssueAnalysis,
) -> OperationalRoute | None:
    clean = strip_log_timestamp(analysis.representative_line)
    lower = clean.lower()

    for rule in service.operational_dependency_rules:
        pattern = rule.pattern.lower()
        if fnmatch(lower, pattern) or fnmatch(lower, f"*{pattern}*"):
            return operational_route(
                service=service,
                dependency=rule.dependency,
                reason=rule.reason,
                signature=rule.pattern,
            )

    if "homelab.client.homelabfunctionserror" in lower and "500 internal server error" in lower:
        return operational_route(
            service=service,
            dependency="homelab-functions",
            reason="homelab-functions returned 500, so the event is classified as downstream service unavailability.",
            signature="homelab.client.HomelabFunctionsError: 500 Internal Server Error",
        )

    transient_network = (
        "cannot connect" in lower
        or "connection refused" in lower
        or "temporary failure" in lower
        or "timed out" in lower
        or "timeout" in lower
    )
    if ".ui.nabu.casa" in lower and transient_network:
        return operational_route(
            service=service,
            dependency="home-assistant-cloud",
            reason="Home Assistant cloud endpoint was temporarily unreachable.",
            signature="home-assistant-cloud transient connection failure",
        )

    return None


def operational_route(
    *,
    service: ServiceMetadata,
    dependency: str,
    reason: str,
    signature: str,
) -> OperationalRoute:
    digest = hashlib.sha256(
        "|".join((service.name, dependency, normalize_for_key(signature))).encode("utf-8")
    ).hexdigest()
    return OperationalRoute(
        fingerprint=f"operational:{service.name}:{digest}",
        dependency=dependency,
        reason=reason,
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
    normalized = strip_log_timestamp(value).lower()
    normalized = re.sub(r"https?://\S+", "<url>", normalized)
    normalized = re.sub(r"\b[a-z0-9.-]+\.(ui\.nabu\.casa|local|lan|internal)\b", "<host>", normalized)
    normalized = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", normalized)
    normalized = re.sub(r"\b[a-z0-9_-]{24,}\b", "<id>", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def recurrence_summary_lines(recurrence: RecurrenceSummary) -> str:
    average_gap = f"{recurrence.average_gap_seconds}s" if recurrence.average_gap_seconds is not None else "n/a"
    return "\n".join(
        [
            f"- Total observations: `{recurrence.total_count}`",
            f"- First seen: `{recurrence.first_seen_at or 'unknown'}`",
            f"- Latest seen: `{recurrence.latest_seen_at or 'unknown'}`",
            f"- Last 24h / 7d / 14d: `{recurrence.last_24h_count}` / `{recurrence.last_7d_count}` / `{recurrence.last_14d_count}`",
            f"- Average gap: `{average_gap}`",
            f"- Likelihood next 14 days: `{recurrence.likelihood}`",
        ]
    )


def dispatch_fingerprint(state_key: str) -> str:
    return hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:16]


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
