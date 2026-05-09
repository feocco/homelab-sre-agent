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

    def collect(self, *, container_id: str | None, container_name: str | None, tail: int) -> str:
        return self.text


def make_config(path: Path, *, dry_run: bool = True) -> Config:
    return Config(
        state_path=path / "state.sqlite3",
        service_metadata_path=path / "services.yaml",
        incident_token="secret",
        github_token=None,
        github_api_url="https://api.github.com",
        default_issue_repo="feocco/homelab-config",
        dry_run=dry_run,
        docker_log_tail=200,
        investigation_cooldown_seconds=86400,
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
    def make_service(self, metadata: str, *, logs: str = "ERROR token=abc failed"):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name)
        (path / "services.yaml").write_text(metadata, encoding="utf-8")
        config = make_config(path)
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
"""
        )
        self.addCleanup(tmp.cleanup)

        first = service.handle_incident(payload(fingerprint="fingerprint-one"))
        second = service.handle_incident(payload(fingerprint="fingerprint-two"))

        self.assertTrue(first["dispatch"]["attempted"])
        self.assertEqual(second["dispatch"]["reason"], "repo daily limit")
        self.assertEqual(
            [action["action"] for action in github.dry_run_actions],
            ["create_issue", "repository_dispatch", "create_issue"],
        )

    def test_issue_body_redacts_logs(self) -> None:
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
        self.assertIn("HA_LONG_LIVED_TOKEN=<redacted>", body)
