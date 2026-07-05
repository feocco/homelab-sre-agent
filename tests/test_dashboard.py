from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from unittest import TestCase
from urllib.error import HTTPError
from urllib.request import urlopen

from homelab_sre_agent.config import Config
from homelab_sre_agent.dashboard import DashboardEnrichment, render_family_detail, render_family_from_state, render_index_from_state
from homelab_sre_agent.docker_logs import DockerLogCollector
from homelab_sre_agent.github import GitHubClient, GitHubIssueDetails, GitHubPullRequest
from homelab_sre_agent.metadata import load_catalog
from homelab_sre_agent.server import SREServer
from homelab_sre_agent.service import SREService
from homelab_sre_agent.state import StateStore


NOW = datetime(2026, 5, 9, 5, 0, tzinfo=timezone.utc)


class FakeLogs(DockerLogCollector):
    def collect_many(self, **kwargs) -> str:
        return "ERROR failed"


class FailingDashboardGitHub:
    def get_issue_details(self, *, repo: str, issue_number: int) -> GitHubIssueDetails | None:
        raise RuntimeError("github unavailable")

    def find_pull_request_by_branch(
        self,
        *,
        repo: str,
        branch: str,
        state: str = "all",
    ) -> GitHubPullRequest | None:
        raise RuntimeError("github unavailable")


class RecordingGitHub(GitHubClient):
    def __init__(self) -> None:
        super().__init__(token="token", api_url="https://api.github.com", dry_run=False, timeout_seconds=10)
        self.requests: list[tuple[str, str]] = []

    def _request(self, method, path, payload, *, expect_json=True, not_found_ok=False):
        self.requests.append((method, path))
        if path.endswith("/issues/42"):
            return {
                "number": 42,
                "html_url": "https://github.com/feocco/plant-monitor/issues/42",
                "title": "SRE: plant-monitor",
                "state": "open",
                "labels": [{"name": "homelab-sre"}, {"name": "error"}],
            }
        if path.startswith("/repos/feocco/plant-monitor/pulls?"):
            return [
                {
                    "number": 7,
                    "html_url": "https://github.com/feocco/plant-monitor/pull/7",
                    "title": "SRE investigation",
                    "state": "closed",
                    "head": {"ref": "codex/sre-42-abcdef"},
                }
            ]
        return None


def make_config(path: Path) -> Config:
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
        dry_run=True,
        docker_log_tail=200,
        docker_log_lookback_seconds=600,
        episode_window_seconds=120,
        diagnostic_max_bytes=1_000_000,
        investigation_cooldown_seconds=86400,
        issue_comment_cooldown_seconds=3600,
        operational_notification_cooldown_seconds=3600,
        codex_global_daily_limit=3,
        approval_poll_seconds=300,
        issue_notifications_enabled=False,
        phone_approvals_enabled=False,
        diagnostic_publish_enabled=False,
        diagnostic_s3_bucket=None,
        diagnostic_s3_region=None,
        diagnostic_s3_prefix="diagnostics",
        diagnostic_url_ttl_seconds=3600,
        diagnostic_retention_days=14,
        service_host="127.0.0.1",
        service_port=0,
        http_timeout_seconds=10,
        log_level="INFO",
    )


def seed_dashboard_state(state: StateStore) -> str:
    fingerprint = "episode:plant-monitor:fingerprint"
    state.claim_issue_for_incident(
        fingerprint=fingerprint,
        service_name="plant-monitor",
        issue_repo="feocco/plant-monitor",
        now=NOW - timedelta(days=2),
        claim_ttl_seconds=300,
    )
    state.set_issue(
        fingerprint=fingerprint,
        issue_number=42,
        issue_url="https://github.com/feocco/plant-monitor/issues/42",
        now=NOW - timedelta(days=2),
    )
    state.record_observation(
        fingerprint=fingerprint,
        service_name="plant-monitor",
        issue_repo="feocco/plant-monitor",
        source_fingerprint="source-one",
        severity="ERROR",
        observed_at=NOW - timedelta(days=2),
        representative_line="ERROR first failure",
        diagnostic_reference="/nas/diagnostics/plant-monitor/first.log",
    )
    state.record_observation(
        fingerprint=fingerprint,
        service_name="plant-monitor",
        issue_repo="feocco/plant-monitor",
        source_fingerprint="source-two",
        severity="ERROR",
        observed_at=NOW - timedelta(hours=1),
        representative_line="ERROR <script>alert(1)</script>",
        diagnostic_reference="/nas/diagnostics/plant-monitor/latest.log",
    )
    state.record_dispatch(
        source_repo="feocco/plant-monitor",
        service_name="plant-monitor",
        fingerprint=fingerprint,
        now=NOW - timedelta(minutes=30),
    )
    token = state.create_phone_approval_token(
        fingerprint=fingerprint,
        service_name="plant-monitor",
        issue_repo="feocco/plant-monitor",
        issue_number=42,
        issue_url="https://github.com/feocco/plant-monitor/issues/42",
        now=NOW - timedelta(minutes=45),
    )
    state.consume_phone_approval_token(token, now=NOW - timedelta(minutes=40), max_age_seconds=86400)
    state.record_operational_observation(
        fingerprint="operational:bedtime:fingerprint",
        service_name="bedtime",
        issue_repo="feocco/homelab-config",
        dependency="homelab-functions",
        reason="homelab-functions returned 500.",
        observed_at=NOW - timedelta(minutes=10),
        line="homelab-functions 500",
        diagnostic_reference="/nas/diagnostics/bedtime/latest.log",
    )
    state.record_operational_observation(
        fingerprint="operational:bedtime:fingerprint",
        service_name="bedtime",
        issue_repo="feocco/homelab-config",
        dependency="homelab-functions",
        reason="homelab-functions returned 500.",
        observed_at=NOW - timedelta(minutes=5),
        line="homelab-functions 500 again",
        diagnostic_reference="/nas/diagnostics/bedtime/latest.log",
    )
    return fingerprint


class DashboardStateTests(TestCase):
    def test_dashboard_families_include_incident_and_operational_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            fingerprint = seed_dashboard_state(state)

            families = state.list_dashboard_families(now=NOW)
            incident = next(item for item in families if item.fingerprint == fingerprint)
            operational = next(item for item in families if item.family_type == "operational")

            self.assertEqual(incident.family_type, "incident")
            self.assertEqual(incident.count, 2)
            self.assertEqual(incident.last_24h_count, 1)
            self.assertEqual(incident.last_7d_count, 2)
            self.assertEqual(incident.last_14d_count, 2)
            self.assertEqual(incident.source_fingerprint_count, 2)
            self.assertEqual(incident.dispatch_count, 1)
            self.assertEqual(incident.approval_token_count, 1)
            self.assertEqual(incident.used_approval_token_count, 1)
            self.assertIn("<script>", incident.latest_line)

            self.assertEqual(operational.service_name, "bedtime")
            self.assertEqual(operational.count, 2)
            self.assertIsNone(operational.last_7d_count)
            self.assertEqual(operational.dependency, "homelab-functions")

    def test_dashboard_family_detail_attaches_examples_dispatches_and_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            fingerprint = seed_dashboard_state(state)

            detail = state.get_dashboard_family(family_type="incident", fingerprint=fingerprint, now=NOW)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(len(detail.observations), 2)
            self.assertEqual(detail.observations[0].representative_line, "ERROR <script>alert(1)</script>")
            self.assertEqual(len(detail.dispatches), 1)
            self.assertEqual(detail.dispatches[0].source_repo, "feocco/plant-monitor")
            self.assertEqual(len(detail.approvals), 1)
            self.assertIsNotNone(detail.approvals[0].used_at)

    def test_dashboard_html_escapes_log_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            fingerprint = seed_dashboard_state(state)
            detail = state.get_dashboard_family(family_type="incident", fingerprint=fingerprint, now=NOW)
            assert detail is not None

            html = render_family_detail(detail, DashboardEnrichment(issue=None, pull_requests=(), expected_branches=(), error=None))

            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
            self.assertNotIn("<script>alert(1)</script>", html)

    def test_dashboard_index_links_family_separately_from_service_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            seed_dashboard_state(state)

            html = render_index_from_state(state, "")

            self.assertIn("<th>family</th><th>service</th>", html)
            self.assertIn(
                '<a class="family-link" href="/incidents/family?type=incident&amp;fingerprint=episode%3Aplant-monitor%3Afingerprint">ERROR &lt;script&gt;alert(1)&lt;/script&gt;</a>',
                html,
            )
            self.assertIn('<a class="service-link" href="/incidents?service=plant-monitor">plant-monitor</a>', html)
            self.assertNotIn(
                '<a href="/incidents/family?type=incident&amp;fingerprint=episode%3Aplant-monitor%3Afingerprint">plant-monitor</a>',
                html,
            )
            self.assertIn("homelab-functions operational dependency", html)

    def test_github_enrichment_unavailable_still_renders_family_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(Path(tmp) / "state.sqlite3")
            fingerprint = seed_dashboard_state(state)

            html = render_family_from_state(
                state,
                FailingDashboardGitHub(),
                f"type=incident&fingerprint={fingerprint}",
            )

            self.assertIsNotNone(html)
            assert html is not None
            self.assertIn("github unavailable", html)
            self.assertIn("ERROR &lt;script&gt;alert(1)&lt;/script&gt;", html)


class DashboardServerTests(TestCase):
    def make_service(self, path: Path) -> tuple[Config, SREService]:
        config = make_config(path)
        config.service_metadata_path.write_text(
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
        state = StateStore(config.state_path)
        seed_dashboard_state(state)
        service = SREService(
            config=config,
            catalog=load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo),
            state=state,
            github=GitHubClient(token=None, api_url=config.github_api_url, dry_run=True, timeout_seconds=10),
            logs=FakeLogs(),
        )
        return config, service

    def serve(self, service: SREService, config: Config):
        handler = SREServer(config=config, service=service)._handler()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.shutdown)
        return httpd

    def test_incidents_route_returns_html_with_incident_and_operational_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, service = self.make_service(Path(tmp))
            httpd = self.serve(service, config)
            port = httpd.server_address[1]

            with urlopen(f"http://127.0.0.1:{port}/incidents") as response:
                body = response.read().decode("utf-8")

            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn("plant-monitor", body)
            self.assertIn("bedtime", body)
            self.assertIn("operational", body)

    def test_family_route_returns_detail_and_unknown_family_404s(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, service = self.make_service(Path(tmp))
            fingerprint = "episode:plant-monitor:fingerprint"
            httpd = self.serve(service, config)
            port = httpd.server_address[1]

            with urlopen(f"http://127.0.0.1:{port}/incidents/family?type=incident&fingerprint={fingerprint}") as response:
                body = response.read().decode("utf-8")

            self.assertEqual(response.status, 200)
            self.assertIn("Family", body)
            self.assertIn("feocco/plant-monitor#42", body)

            with self.assertRaises(HTTPError) as raised:
                urlopen(f"http://127.0.0.1:{port}/incidents/family?type=incident&fingerprint=missing")
            self.assertEqual(raised.exception.code, 404)


class GitHubDashboardTests(TestCase):
    def test_read_only_issue_and_pull_request_lookup_parse_github_responses(self) -> None:
        github = RecordingGitHub()

        issue = github.get_issue_details(repo="feocco/plant-monitor", issue_number=42)
        pull_request = github.find_pull_request_by_branch(
            repo="feocco/plant-monitor",
            branch="codex/sre-42-abcdef",
            state="all",
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.state, "open")
        self.assertEqual(issue.labels, ("homelab-sre", "error"))
        self.assertIsNotNone(pull_request)
        assert pull_request is not None
        self.assertEqual(pull_request.number, 7)
        self.assertEqual(pull_request.state, "closed")
        self.assertIn("/issues/42", github.requests[0][1])
        self.assertIn("pulls?state=all", github.requests[1][1])

    def test_read_only_github_lookup_returns_none_in_dry_run(self) -> None:
        github = GitHubClient(token=None, api_url="https://api.github.com", dry_run=True, timeout_seconds=10)

        self.assertIsNone(github.get_issue_details(repo="feocco/plant-monitor", issue_number=42))
        self.assertIsNone(
            github.find_pull_request_by_branch(
                repo="feocco/plant-monitor",
                branch="codex/sre-42-abcdef",
            )
        )
