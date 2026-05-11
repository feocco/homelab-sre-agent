from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import TestCase

from homelab_sre_agent.config import Config
from homelab_sre_agent.docker_logs import DockerLogCollector
from homelab_sre_agent.github import GitHubClient, GitHubIssue, IssueResult
from homelab_sre_agent.metadata import load_catalog
from homelab_sre_agent.redact import redact_text
from homelab_sre_agent.service import AUTOFIX_APPROVED_LABEL, AUTOFIX_PENDING_LABEL, AUTOFIX_STARTED_LABEL, SREService
from homelab_sre_agent.state import StateStore


class FakeLogs(DockerLogCollector):
    def __init__(self, text: str) -> None:
        self.text = text

    def collect_many(
        self,
        *,
        container_names: tuple[str, ...],
        incident_container_id: str | None,
        incident_container_name: str,
        tail: int,
        since=None,
    ) -> str:
        return self.text


class FakeGitHub(GitHubClient):
    def __init__(self) -> None:
        super().__init__(token=None, api_url="https://api.github.com", dry_run=True, timeout_seconds=10)
        self.next_issue_number = 2
        self.issues: dict[tuple[str, int], GitHubIssue] = {}
        self.ensured_labels: list[tuple[str, str]] = []

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str]) -> IssueResult:
        issue = GitHubIssue(
            repo=repo,
            number=self.next_issue_number,
            url=f"https://github.com/{repo}/issues/{self.next_issue_number}",
            title=title,
            labels=tuple(labels),
        )
        self.next_issue_number += 1
        self.issues[(repo, issue.number)] = issue
        self.dry_run_actions.append(
            {"action": "create_issue", "repo": repo, "title": title, "body": body, "labels": labels}
        )
        return IssueResult(repo=repo, number=issue.number, url=issue.url)

    def list_open_issues_with_label(self, *, repo: str, label: str) -> list[GitHubIssue]:
        return [issue for issue in self.issues.values() if issue.repo == repo and label in issue.labels]

    def add_issue_labels(self, *, repo: str, issue_number: int, labels: list[str]) -> None:
        key = (repo, issue_number)
        issue = self.issues[key]
        self.issues[key] = GitHubIssue(
            repo=issue.repo,
            number=issue.number,
            url=issue.url,
            title=issue.title,
            labels=tuple(sorted(set(issue.labels).union(labels))),
        )
        self.dry_run_actions.append(
            {"action": "add_issue_labels", "repo": repo, "issue_number": issue_number, "labels": labels}
        )

    def remove_issue_label(self, *, repo: str, issue_number: int, label: str) -> None:
        key = (repo, issue_number)
        issue = self.issues[key]
        self.issues[key] = GitHubIssue(
            repo=issue.repo,
            number=issue.number,
            url=issue.url,
            title=issue.title,
            labels=tuple(item for item in issue.labels if item != label),
        )
        self.dry_run_actions.append(
            {"action": "remove_issue_label", "repo": repo, "issue_number": issue_number, "label": label}
        )

    def ensure_label(self, *, repo: str, name: str, color: str, description: str) -> None:
        self.ensured_labels.append((repo, name))


def make_config(
    path: Path,
    *,
    dry_run: bool = True,
    episode_window_seconds: int = 120,
    investigation_cooldown_seconds: int = 86400,
) -> Config:
    return Config(
        state_path=path / "state.sqlite3",
        service_metadata_path=path / "services.yaml",
        diagnostic_dir=path / "diagnostics",
        diagnostic_reference_root="/nas/diagnostics",
        incident_token="secret",
        github_token=None,
        github_api_url="https://api.github.com",
        default_issue_repo="feocco/homelab-config",
        dry_run=dry_run,
        docker_log_tail=200,
        docker_log_lookback_seconds=600,
        episode_window_seconds=episode_window_seconds,
        diagnostic_max_bytes=1_000_000,
        investigation_cooldown_seconds=investigation_cooldown_seconds,
        codex_global_daily_limit=3,
        approval_poll_seconds=300,
        service_host="127.0.0.1",
        service_port=8094,
        http_timeout_seconds=10,
        log_level="INFO",
    )


def payload(container: str = "plant-monitor", fingerprint: str = "abc123def456") -> dict:
    return {
        "version": 1,
        "source": "homelab-log-watcher",
        "detected_at": "2026-05-09T05:00:00+00:00",
        "incident": {
            "container_id": "container-id",
            "container_name": container,
            "image": "ghcr.io/feocco/plant-monitor:latest",
            "severity": "ERROR",
            "matched_pattern": "ERROR",
            "line": "ERROR token=secret-value failed",
            "normalized_line": "ERROR token=<id> failed",
            "fingerprint": fingerprint,
            "occurred_at": "2026-05-09T05:00:00+00:00",
        },
    }


class MetadataTests(TestCase):
    def test_container_and_image_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services.yaml"
            path.write_text(
                """
defaults:
  issue_repo: feocco/homelab-config
services:
  plant-monitor:
    containers: [plant-monitor, plant-monitor-temporal]
    images: ["ghcr.io/feocco/plant-monitor:*"]
    source:
      repo: feocco/plant-monitor
    deploy:
      path: plant-monitor
""",
                encoding="utf-8",
            )

            catalog = load_catalog(path, default_issue_repo="feocco/homelab-config")

            by_container = catalog.match(container_name="plant-monitor-temporal", image="unknown")
            by_image = catalog.match(container_name="renamed", image="ghcr.io/feocco/plant-monitor:latest")
            self.assertEqual(by_container.source_repo, "feocco/plant-monitor")
            self.assertEqual(by_image.name, "plant-monitor")
            self.assertFalse(by_container.sre_enabled)


class RedactionTests(TestCase):
    def test_redacts_common_secret_shapes(self) -> None:
        text = "Authorization: Bearer abc.def token=my-token https://user:pass@example.test/path"
        redacted = redact_text(text)

        self.assertNotIn("abc.def", redacted)
        self.assertNotIn("my-token", redacted)
        self.assertNotIn("user:pass", redacted)
        self.assertIn("Authorization=<redacted>", redacted)


class ServiceTests(TestCase):
    def make_service(
        self,
        metadata: str,
        *,
        logs: str = "ERROR token=abc failed",
        episode_window_seconds: int = 120,
        investigation_cooldown_seconds: int = 86400,
        github: GitHubClient | None = None,
    ):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name)
        (path / "services.yaml").write_text(metadata, encoding="utf-8")
        config = make_config(
            path,
            episode_window_seconds=episode_window_seconds,
            investigation_cooldown_seconds=investigation_cooldown_seconds,
        )
        catalog = load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo)
        github = github or GitHubClient(token=None, api_url=config.github_api_url, dry_run=True, timeout_seconds=10)
        service = SREService(
            config=config,
            catalog=catalog,
            state=StateStore(config.state_path),
            github=github,
            logs=FakeLogs(logs),
        )
        return tmp, service, github

    def test_metadata_can_reload_between_incidents(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        metadata_path = path / "services.yaml"
        metadata_path.write_text(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
""",
            encoding="utf-8",
        )
        config = make_config(path)
        github = GitHubClient(token=None, api_url=config.github_api_url, dry_run=True, timeout_seconds=10)
        service = SREService(
            config=config,
            catalog_loader=lambda: load_catalog(metadata_path, default_issue_repo=config.default_issue_repo),
            state=StateStore(config.state_path),
            github=github,
            logs=FakeLogs("ERROR failed"),
        )

        first = service.handle_incident(payload(fingerprint="first-fingerprint"))
        metadata_path.write_text(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/hello-nas
    sre:
      enabled: true
""",
            encoding="utf-8",
        )
        second = service.handle_incident(payload(fingerprint="second-fingerprint"))

        self.assertEqual(first["issue"]["repo"], "feocco/plant-monitor")
        self.assertEqual(second["issue"]["repo"], "feocco/hello-nas")

    def test_unknown_service_is_ignored_without_issue(self) -> None:
        tmp, service, github = self.make_service("services: {}\n")
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload(container="mystery-service"))

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "sre disabled")
        self.assertEqual(result["service"], "unknown-mystery-service")
        self.assertEqual(github.dry_run_actions, [])

    def test_service_requires_explicit_sre_enabled(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
"""
        )
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload())

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "sre disabled")
        self.assertEqual(github.dry_run_actions, [])

    def test_duplicate_fingerprint_comments_existing_issue(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
"""
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload())
        service.handle_incident(payload())

        self.assertEqual([action["action"] for action in github.dry_run_actions], ["create_issue", "comment_issue"])

    def test_autofix_requires_approval_and_poll_dispatches(self) -> None:
        github = FakeGitHub()
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
      autofix: true
      repo_daily_limit: 1
""",
            episode_window_seconds=0,
            investigation_cooldown_seconds=0,
            github=github,
        )
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload(fingerprint="fingerprint-one"))
        issue_key = ("feocco/plant-monitor", result["issue"]["number"])

        self.assertEqual(result["dispatch"]["reason"], "autofix approval required")
        self.assertIn(AUTOFIX_PENDING_LABEL, github.issues[issue_key].labels)
        self.assertNotIn("repository_dispatch", [action["action"] for action in github.dry_run_actions])

        github.add_issue_labels(repo=issue_key[0], issue_number=issue_key[1], labels=[AUTOFIX_APPROVED_LABEL])
        poll_result = service.poll_autofix_approvals()

        self.assertEqual(poll_result["processed"], 1)
        self.assertIn("repository_dispatch", [action["action"] for action in github.dry_run_actions])
        self.assertIn(AUTOFIX_STARTED_LABEL, github.issues[issue_key].labels)
        self.assertNotIn(AUTOFIX_APPROVED_LABEL, github.issues[issue_key].labels)
        self.assertNotIn(AUTOFIX_PENDING_LABEL, github.issues[issue_key].labels)

    def test_poll_does_not_reensure_labels_without_approved_issues(self) -> None:
        github = FakeGitHub()
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
      autofix: true
      repo_daily_limit: 1
""",
            github=github,
        )
        self.addCleanup(tmp.cleanup)

        service.ensure_autofix_labels()
        self.assertIn(("feocco/plant-monitor", AUTOFIX_APPROVED_LABEL), github.ensured_labels)

        github.ensured_labels.clear()
        poll_result = service.poll_autofix_approvals()

        self.assertEqual(poll_result["processed"], 0)
        self.assertEqual(github.ensured_labels, [])

    def test_approved_autofix_obeys_repo_daily_limit(self) -> None:
        github = FakeGitHub()
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
      autofix: true
      repo_daily_limit: 1
""",
            episode_window_seconds=0,
            investigation_cooldown_seconds=0,
            github=github,
        )
        self.addCleanup(tmp.cleanup)

        first = service.handle_incident(payload(fingerprint="fingerprint-one"))
        second_payload = payload(fingerprint="fingerprint-two")
        second_payload["incident"]["severity"] = "WARN"
        second_payload["incident"]["matched_pattern"] = "WARN"
        second_payload["incident"]["line"] = "WARN another failure"
        second = service.handle_incident(second_payload)
        first_issue_key = ("feocco/plant-monitor", first["issue"]["number"])
        second_issue_key = ("feocco/plant-monitor", second["issue"]["number"])

        github.add_issue_labels(repo=first_issue_key[0], issue_number=first_issue_key[1], labels=[AUTOFIX_APPROVED_LABEL])
        service.poll_autofix_approvals()
        github.add_issue_labels(
            repo=second_issue_key[0],
            issue_number=second_issue_key[1],
            labels=[AUTOFIX_APPROVED_LABEL],
        )
        poll_result = service.poll_autofix_approvals()

        self.assertEqual(poll_result["results"][0]["status"], "blocked")
        self.assertEqual(poll_result["results"][0]["reason"], "repo daily limit")

    def test_issue_body_uses_summary_and_local_diagnostic_reference(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
""",
            logs="ERROR HA_LONG_LIVED_TOKEN=super-secret-value failed",
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload())

        body = github.dry_run_actions[0]["body"]
        self.assertNotIn("super-secret-value", body)
        self.assertIn("Relevant redacted log line:", body)
        self.assertIn("HA_LONG_LIVED_TOKEN=<redacted>", body)
        self.assertIn("Full local diagnostic bundle: `/nas/diagnostics/plant-monitor/", body)
        self.assertNotIn("## Recent Logs", body)

    def test_traceback_lines_within_episode_update_existing_issue(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
""",
            logs="ERROR request failed\nTraceback\nNameError: name 'CANARY_STATUS' is not defined",
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload(fingerprint="fingerprint-one"))
        service.handle_incident(payload(fingerprint="fingerprint-two"))

        self.assertEqual([action["action"] for action in github.dry_run_actions], ["create_issue", "comment_issue"])
        body = github.dry_run_actions[0]["body"]
        self.assertIn("hit `NameError`", body)
        self.assertIn("name 'CANARY_STATUS' is not defined", body)
