from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import TestCase

from homelab_sre_agent.config import Config
from homelab_sre_agent.docker_logs import DockerLogCollector
from homelab_sre_agent.github import GitHubClient
from homelab_sre_agent.metadata import load_catalog
from homelab_sre_agent.redact import redact_text
from homelab_sre_agent.service import SREService
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
        github = GitHubClient(token=None, api_url=config.github_api_url, dry_run=True, timeout_seconds=10)
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
""",
            encoding="utf-8",
        )
        second = service.handle_incident(payload(fingerprint="second-fingerprint"))

        self.assertEqual(first["issue"]["repo"], "feocco/plant-monitor")
        self.assertEqual(second["issue"]["repo"], "feocco/hello-nas")

    def test_unknown_service_creates_homelab_config_issue_without_dispatch(self) -> None:
        tmp, service, github = self.make_service("services: {}\n")
        self.addCleanup(tmp.cleanup)

        result = service.handle_incident(payload(container="mystery-service"))

        self.assertEqual(result["issue"]["repo"], "feocco/homelab-config")
        self.assertEqual(result["dispatch"]["reason"], "unknown service")
        self.assertEqual(github.dry_run_actions[0]["action"], "create_issue")

    def test_duplicate_fingerprint_comments_existing_issue(self) -> None:
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

        service.handle_incident(payload())
        service.handle_incident(payload())

        self.assertEqual([action["action"] for action in github.dry_run_actions], ["create_issue", "comment_issue"])

    def test_autofix_dispatch_obeys_repo_daily_limit(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
    sre:
      autofix: true
      repo_daily_limit: 1
""",
            episode_window_seconds=0,
            investigation_cooldown_seconds=0,
        )
        self.addCleanup(tmp.cleanup)

        first = service.handle_incident(payload(fingerprint="fingerprint-one"))
        second_payload = payload(fingerprint="fingerprint-two")
        second_payload["incident"]["severity"] = "WARN"
        second_payload["incident"]["matched_pattern"] = "WARN"
        second_payload["incident"]["line"] = "WARN another failure"
        second = service.handle_incident(second_payload)

        self.assertTrue(first["dispatch"]["attempted"])
        self.assertEqual(second["dispatch"]["reason"], "repo daily limit")
        self.assertEqual(
            [action["action"] for action in github.dry_run_actions],
            ["create_issue", "repository_dispatch", "create_issue"],
        )

    def test_issue_body_uses_summary_and_local_diagnostic_reference(self) -> None:
        tmp, service, github = self.make_service(
            """
services:
  plant-monitor:
    containers: [plant-monitor]
    source:
      repo: feocco/plant-monitor
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
