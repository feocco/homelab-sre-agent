from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

from homelab_sre_agent.config import Config
from homelab_sre_agent.docker_logs import DockerLogCollector
from homelab_sre_agent.github import GitHubAppInstallationAuth, GitHubClient, GitHubIssue, IssueResult, github_client_from_config
from homelab_sre_agent.metadata import load_catalog
from homelab_sre_agent.notifications import (
    PHONE_APPROVAL_TOKEN_TTL_SECONDS,
    SRE_APPROVE_ACTION_PREFIX,
    IssueNotifier,
)
from homelab_sre_agent.redact import redact_text
from homelab_sre_agent.service import (
    AUTOFIX_APPROVED_LABEL,
    AUTOFIX_BLOCKED_LABEL,
    AUTOFIX_PENDING_LABEL,
    AUTOFIX_STARTED_LABEL,
    HUMAN_INVESTIGATING_LABEL,
    SRE_LABEL,
    Incident,
    SREService,
    issue_title,
)
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


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 5, 9, 5, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


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

    def find_open_issue_by_title(self, *, repo: str, title: str, label: str | None = None) -> IssueResult | None:
        for issue in self.issues.values():
            if issue.repo != repo or issue.title != title:
                continue
            if label is not None and label not in issue.labels:
                continue
            return IssueResult(repo=repo, number=issue.number, url=issue.url)
        return None

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


class SlowFakeGitHub(FakeGitHub):
    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str]) -> IssueResult:
        time.sleep(0.05)
        return super().create_issue(repo=repo, title=title, body=body, labels=labels)


class FakeNotifier:
    def __init__(self) -> None:
        self.calls = []

    def notify(self, title: str, message: str, **kwargs):
        self.calls.append((title, message, kwargs))
        return {"status": "sent"}


def make_config(
    path: Path,
    *,
    dry_run: bool = True,
    episode_window_seconds: int = 120,
    investigation_cooldown_seconds: int = 86400,
    issue_comment_cooldown_seconds: int = 3600,
    issue_notifications_enabled: bool = False,
    phone_approvals_enabled: bool = False,
    codex_global_daily_limit: int = 3,
) -> Config:
    return Config(
        state_path=path / "state.sqlite3",
        service_metadata_path=path / "services.yaml",
        diagnostic_dir=path / "diagnostics",
        diagnostic_reference_root="/nas/diagnostics",
        incident_token="secret",
        github_token=None,
        github_auth_mode="token",
        github_app_id=None,
        github_app_installation_id=None,
        github_app_private_key=None,
        github_api_url="https://api.github.com",
        default_issue_repo="feocco/homelab-config",
        dry_run=dry_run,
        docker_log_tail=200,
        docker_log_lookback_seconds=600,
        episode_window_seconds=episode_window_seconds,
        diagnostic_max_bytes=1_000_000,
        investigation_cooldown_seconds=investigation_cooldown_seconds,
        issue_comment_cooldown_seconds=issue_comment_cooldown_seconds,
        codex_global_daily_limit=codex_global_daily_limit,
        approval_poll_seconds=300,
        issue_notifications_enabled=issue_notifications_enabled,
        phone_approvals_enabled=phone_approvals_enabled,
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


class ConfigTests(TestCase):
    def test_config_decodes_github_app_private_key_b64(self) -> None:
        encoded_key = base64.b64encode(b"private-key").decode("ascii")

        with patch.dict(os.environ, {"GITHUB_AUTH_MODE": "app", "GITHUB_APP_PRIVATE_KEY_B64": encoded_key}, clear=True):
            config = Config.from_env()

        self.assertEqual(config.github_auth_mode, "app")
        self.assertEqual(config.github_app_private_key, "private-key")

    def test_github_app_auth_requires_complete_config_when_not_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), dry_run=False)
            config = Config(
                **{
                    **config.__dict__,
                    "github_auth_mode": "app",
                }
            )

            with self.assertRaisesRegex(ValueError, "GITHUB_APP_ID"):
                github_client_from_config(config)


class FakeAppAuth(GitHubAppInstallationAuth):
    def __init__(self) -> None:
        self.now = 1_779_000_000.0
        self.requests: list[str] = []
        super().__init__(
            app_id="123",
            installation_id="456",
            private_key="unused",
            api_url="https://api.github.com",
            timeout_seconds=10,
            now_func=lambda: self.now,
        )

    def _make_jwt(self, now: float) -> str:
        return f"jwt-{int(now)}"

    def _post_installation_token(self, jwt_token: str) -> dict:
        self.requests.append(jwt_token)
        expires = datetime.fromtimestamp(self.now + 3600, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"token": f"token-{len(self.requests)}", "expires_at": expires}


class GitHubAppAuthTests(TestCase):
    def test_installation_token_is_cached_until_near_expiration(self) -> None:
        auth = FakeAppAuth()

        first = auth.token()
        second = auth.token()
        auth.now += 3400
        third = auth.token()

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-1")
        self.assertEqual(third, "token-2")
        self.assertEqual(auth.requests, ["jwt-1779000000", "jwt-1779003400"])


class ServiceTests(TestCase):
    def make_service(
        self,
        metadata: str,
        *,
        logs: str = "ERROR token=abc failed",
        episode_window_seconds: int = 120,
        investigation_cooldown_seconds: int = 86400,
        issue_comment_cooldown_seconds: int = 3600,
        github: GitHubClient | None = None,
        issue_notifier=None,
        issue_notifications_enabled: bool = False,
        phone_approvals_enabled: bool = False,
        codex_global_daily_limit: int = 3,
        now_func=None,
    ):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name)
        (path / "services.yaml").write_text(metadata, encoding="utf-8")
        config = make_config(
            path,
            episode_window_seconds=episode_window_seconds,
            investigation_cooldown_seconds=investigation_cooldown_seconds,
            issue_comment_cooldown_seconds=issue_comment_cooldown_seconds,
            issue_notifications_enabled=issue_notifications_enabled,
            phone_approvals_enabled=phone_approvals_enabled,
            codex_global_daily_limit=codex_global_daily_limit,
        )
        catalog = load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo)
        github = github or GitHubClient(token=None, api_url=config.github_api_url, dry_run=True, timeout_seconds=10)
        service = SREService(
            config=config,
            catalog=catalog,
            state=StateStore(config.state_path),
            github=github,
            logs=FakeLogs(logs),
            issue_notifier=issue_notifier,
            now_func=now_func,
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

    def test_repeated_incidents_are_bundled_after_comment_cooldown(self) -> None:
        clock = FakeClock()
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
            now_func=clock.now,
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload(fingerprint="fingerprint-one"))
        service.handle_incident(payload(fingerprint="fingerprint-two"))
        service.handle_incident(payload(fingerprint="fingerprint-three"))
        clock.advance(3601)
        service.handle_incident(payload(fingerprint="fingerprint-four"))

        self.assertEqual([action["action"] for action in github.dry_run_actions], ["create_issue", "comment_issue"])
        self.assertIn("Observed 3 more times since the last update.", github.dry_run_actions[-1]["body"])
        self.assertIn("**homelab-sre-agent**", github.dry_run_actions[-1]["body"])

    def test_concurrent_same_issue_key_creates_one_issue(self) -> None:
        github = SlowFakeGitHub()
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
            github=github,
        )
        self.addCleanup(tmp.cleanup)
        results = []
        errors = []

        def handle() -> None:
            try:
                results.append(service.handle_incident(payload()))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=handle) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)
        self.assertEqual([action["action"] for action in github.dry_run_actions].count("create_issue"), 1)

    def test_existing_open_issue_with_same_title_is_reused(self) -> None:
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
""",
            github=github,
        )
        self.addCleanup(tmp.cleanup)
        incident = Incident.from_payload(payload())
        catalog = service._catalog()
        source = catalog.services[0]
        state_key = service._state_key(
            source,
            incident,
            "ERROR token=abc failed",
            datetime(2026, 5, 9, 5, 0, tzinfo=timezone.utc),
        )
        title = issue_title(source, incident, state_key)
        github.issues[("feocco/plant-monitor", 99)] = GitHubIssue(
            repo="feocco/plant-monitor",
            number=99,
            url="https://github.com/feocco/plant-monitor/issues/99",
            title=title,
            labels=(SRE_LABEL,),
        )

        result = service.handle_incident(payload())

        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["issue"]["number"], 99)
        self.assertNotIn("create_issue", [action["action"] for action in github.dry_run_actions])

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
        self.assertNotIn(AUTOFIX_PENDING_LABEL, github.issues[issue_key].labels)
        self.assertNotIn("repository_dispatch", [action["action"] for action in github.dry_run_actions])

        github.add_issue_labels(repo=issue_key[0], issue_number=issue_key[1], labels=[AUTOFIX_APPROVED_LABEL])
        poll_result = service.poll_autofix_approvals()

        self.assertEqual(poll_result["processed"], 1)
        self.assertIn("repository_dispatch", [action["action"] for action in github.dry_run_actions])
        self.assertNotIn(AUTOFIX_STARTED_LABEL, github.issues[issue_key].labels)
        self.assertNotIn(AUTOFIX_APPROVED_LABEL, github.issues[issue_key].labels)
        self.assertNotIn(AUTOFIX_PENDING_LABEL, github.issues[issue_key].labels)
        self.assertIn("sent `repository_dispatch`", github.dry_run_actions[-1]["body"])
        self.assertIn("**homelab-sre-agent**", github.dry_run_actions[-1]["body"])

    def test_new_issue_sends_phone_notification_with_approval_button(self) -> None:
        github = FakeGitHub()
        fake_notifier = FakeNotifier()
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
""",
            github=github,
            issue_notifier=IssueNotifier(fake_notifier.notify),
            issue_notifications_enabled=True,
            phone_approvals_enabled=True,
        )
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload())

        self.assertEqual(result["status"], "created")
        self.assertEqual(len(fake_notifier.calls), 1)
        title, message, kwargs = fake_notifier.calls[0]
        self.assertEqual(title, "SRE issue - plant-monitor")
        self.assertIn("Issue: feocco/plant-monitor#2", message)
        self.assertEqual(kwargs["url"], "https://github.com/feocco/plant-monitor/issues/2")
        buttons = kwargs["buttons"]
        self.assertEqual(buttons[0]["title"], "Open issue")
        self.assertEqual(buttons[0]["action"], "URI")
        self.assertEqual(buttons[1]["title"], "Approve autofix")
        self.assertTrue(buttons[1]["action"].startswith(f"{SRE_APPROVE_ACTION_PREFIX}::"))

    def test_duplicate_issue_update_does_not_resend_phone_notification(self) -> None:
        fake_notifier = FakeNotifier()
        tmp, service, _github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
""",
            issue_notifier=IssueNotifier(fake_notifier.notify),
            issue_notifications_enabled=True,
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload())
        service.handle_incident(payload())

        self.assertEqual(len(fake_notifier.calls), 1)

    def test_non_autofix_issue_notification_only_opens_issue(self) -> None:
        fake_notifier = FakeNotifier()
        tmp, service, _github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      enabled: true
""",
            issue_notifier=IssueNotifier(fake_notifier.notify),
            issue_notifications_enabled=True,
            phone_approvals_enabled=True,
        )
        self.addCleanup(tmp.cleanup)

        service.handle_incident(payload())

        buttons = fake_notifier.calls[0][2]["buttons"]
        self.assertEqual([button["title"] for button in buttons], ["Open issue"])

    def test_phone_approval_adds_label_and_comment(self) -> None:
        github = FakeGitHub()
        fake_notifier = FakeNotifier()
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
""",
            github=github,
            issue_notifier=IssueNotifier(fake_notifier.notify),
            issue_notifications_enabled=True,
            phone_approvals_enabled=True,
        )
        self.addCleanup(tmp.cleanup)
        result = service.handle_incident(payload())
        issue_key = ("feocco/plant-monitor", result["issue"]["number"])
        approval_action = fake_notifier.calls[0][2]["buttons"][1]["action"]
        token = approval_action.split("::", 1)[1]

        approval_result = service.approve_autofix_from_phone(token)

        self.assertEqual(approval_result["status"], "approved")
        self.assertIn(AUTOFIX_APPROVED_LABEL, github.issues[issue_key].labels)
        self.assertEqual(github.dry_run_actions[-1]["action"], "comment_issue")
        self.assertIn("Autofix approved from the phone notification", github.dry_run_actions[-1]["body"])
        self.assertIn("**homelab-sre-agent**", github.dry_run_actions[-1]["body"])

        duplicate_result = service.approve_autofix_from_phone(token)
        self.assertEqual(duplicate_result["status"], "ignored")

    def test_stale_phone_approval_token_is_ignored(self) -> None:
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
""",
            github=github,
        )
        self.addCleanup(tmp.cleanup)
        old = datetime(2026, 5, 1, tzinfo=timezone.utc)
        token = service.state.create_phone_approval_token(
            fingerprint="fingerprint",
            service_name="plant-monitor",
            issue_repo="feocco/plant-monitor",
            issue_number=22,
            issue_url="https://github.com/feocco/plant-monitor/issues/22",
            now=old,
        )

        result = service.approve_autofix_from_phone(
            token,
            now=old + timedelta(seconds=PHONE_APPROVAL_TOKEN_TTL_SECONDS + 1),
        )

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(github.dry_run_actions, [])

        unknown_result = service.approve_autofix_from_phone("not-a-real-token", now=old)
        self.assertEqual(unknown_result["status"], "ignored")
        self.assertEqual(github.dry_run_actions, [])

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
        self.assertIn(("feocco/plant-monitor", HUMAN_INVESTIGATING_LABEL), github.ensured_labels)
        self.assertNotIn(("feocco/plant-monitor", AUTOFIX_PENDING_LABEL), github.ensured_labels)
        self.assertNotIn(("feocco/plant-monitor", AUTOFIX_STARTED_LABEL), github.ensured_labels)
        self.assertNotIn(("feocco/plant-monitor", AUTOFIX_BLOCKED_LABEL), github.ensured_labels)

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
        self.assertNotIn(AUTOFIX_BLOCKED_LABEL, github.issues[second_issue_key].labels)
        self.assertNotIn(AUTOFIX_APPROVED_LABEL, github.issues[second_issue_key].labels)
        self.assertIn("per-repo Codex dispatch cap", github.dry_run_actions[-1]["body"])
        self.assertIn("**homelab-sre-agent**", github.dry_run_actions[-1]["body"])

    def test_approved_autofix_obeys_global_daily_limit_with_explanatory_comment(self) -> None:
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
      repo_daily_limit: 5
""",
            episode_window_seconds=0,
            investigation_cooldown_seconds=0,
            codex_global_daily_limit=0,
            github=github,
        )
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload(fingerprint="fingerprint-one"))
        issue_key = ("feocco/plant-monitor", result["issue"]["number"])
        github.add_issue_labels(repo=issue_key[0], issue_number=issue_key[1], labels=[AUTOFIX_APPROVED_LABEL])

        poll_result = service.poll_autofix_approvals()

        self.assertEqual(poll_result["results"][0]["status"], "blocked")
        self.assertEqual(poll_result["results"][0]["reason"], "global daily limit")
        self.assertNotIn(AUTOFIX_BLOCKED_LABEL, github.issues[issue_key].labels)
        self.assertNotIn(AUTOFIX_APPROVED_LABEL, github.issues[issue_key].labels)
        self.assertIn("global Codex dispatch cap", github.dry_run_actions[-1]["body"])
        self.assertIn("SRE_CODEX_GLOBAL_DAILY_LIMIT", github.dry_run_actions[-1]["body"])
        self.assertIn("**homelab-sre-agent**", github.dry_run_actions[-1]["body"])

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
        self.assertIn("SRE state fingerprint:", body)
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
            episode_window_seconds=0,
            logs="ERROR request failed\nTraceback\nNameError: name 'CANARY_STATUS' is not defined",
        )
        self.addCleanup(tmp.cleanup)

        traceback_payload = payload(fingerprint="fingerprint-two")
        traceback_payload["incident"]["matched_pattern"] = "Traceback"
        traceback_payload["incident"]["line"] = "Traceback (most recent call last):"
        traceback_payload["incident"]["normalized_line"] = "traceback most recent call last"
        exception_payload = payload(fingerprint="fingerprint-three")
        exception_payload["incident"]["matched_pattern"] = "Exception"
        exception_payload["incident"]["line"] = "NameError: name 'CANARY_STATUS' is not defined"
        exception_payload["incident"]["normalized_line"] = "nameerror name <id> is not defined"

        service.handle_incident(payload(fingerprint="fingerprint-one"))
        service.handle_incident(traceback_payload)
        service.handle_incident(exception_payload)

        self.assertEqual([action["action"] for action in github.dry_run_actions], ["create_issue"])
        body = github.dry_run_actions[0]["body"]
        self.assertIn("hit `NameError`", body)
        self.assertIn("name 'CANARY_STATUS' is not defined", body)
