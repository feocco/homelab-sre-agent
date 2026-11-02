#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


HELPER_PATH = Path(__file__).resolve()
REPO_ROOT = HELPER_PATH.parents[3]
sys.path.insert(0, str(REPO_ROOT))

from homelab_sre_agent.config import Config
from homelab_sre_agent.github import GitHubIssue, IssueResult
from homelab_sre_agent.metadata import load_catalog
from homelab_sre_agent.server import SREServer
from homelab_sre_agent.service import AUTOFIX_APPROVED_LABEL, SREService
from homelab_sre_agent.state import StateStore


ENVELOPE_PROPERTIES = ("version", "source", "detected_at")
INCIDENT_PROPERTIES = (
    "container_id",
    "container_name",
    "image",
    "severity",
    "matched_pattern",
    "line",
    "normalized_line",
    "fingerprint",
    "occurred_at",
)
DISPATCH_BASE_PROPERTIES = (
    "issue_number",
    "issue_url",
    "fingerprint",
    "state_fingerprint",
    "service_name",
    "container_name",
    "deployment_repo",
    "deployment_path",
)
DIAGNOSTIC_PROPERTIES = (
    "url",
    "reference",
    "diagnostic_uploaded_at",
    "diagnostic_expires_at",
)
FIXED_NOW = datetime(2026, 11, 2, 4, 26, tzinfo=timezone.utc)
DEFAULT_EVIDENCE_ROOT = Path(tempfile.gettempdir()) / "homelab-sre-agent-verification-evidence"


class VerificationError(RuntimeError):
    pass


class VerificationLogs:
    def collect_many(self, **kwargs: Any) -> str:
        return "2026-11-02T04:26:00Z ERROR database connection failed"


class VerificationDiagnosticPublisher:
    def upload(
        self,
        *,
        service: Any,
        issue: IssueResult,
        state_key: str,
        diagnostic_reference: str,
        recurrence: Any,
        now: datetime,
    ) -> str:
        return f"verification/{service.name}/issue-{issue.number}/diagnostic.json"

    def presign(self, object_key: str) -> str:
        return f"https://diagnostics.invalid/{object_key}"


class VerificationGitHub:
    def __init__(self, dispatches_path: Path, approval_signal_path: Path) -> None:
        self.dispatches_path = dispatches_path
        self.approval_signal_path = approval_signal_path
        self.issue: GitHubIssue | None = None
        self.lock = threading.RLock()
        self.dispatches: list[dict[str, Any]] = []
        self.approval_consumed = False

    def ensure_label(self, *, repo: str, name: str, color: str, description: str) -> None:
        return None

    def find_open_issue_by_title(
        self,
        *,
        repo: str,
        title: str,
        label: str | None = None,
    ) -> IssueResult | None:
        with self.lock:
            if self.issue is None or self.issue.repo != repo or self.issue.title != title:
                return None
            if label is not None and label not in self.issue.labels:
                return None
            return IssueResult(repo=repo, number=self.issue.number, url=self.issue.url)

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str]) -> IssueResult:
        with self.lock:
            self.issue = GitHubIssue(
                repo=repo,
                number=42,
                url=f"https://github.com/{repo}/issues/42",
                title=title,
                labels=tuple(sorted(set(labels))),
            )
            return IssueResult(repo=repo, number=self.issue.number, url=self.issue.url)

    def list_open_issues_with_label(self, *, repo: str, label: str) -> list[GitHubIssue]:
        with self.lock:
            if label == AUTOFIX_APPROVED_LABEL:
                self._consume_approval_signal()
            if self.issue is None or self.issue.repo != repo or label not in self.issue.labels:
                return []
            return [self.issue]

    def add_issue_labels(self, *, repo: str, issue_number: int, labels: list[str]) -> None:
        with self.lock:
            issue = self._matching_issue(repo, issue_number)
            self.issue = GitHubIssue(
                repo=issue.repo,
                number=issue.number,
                url=issue.url,
                title=issue.title,
                labels=tuple(sorted(set(issue.labels).union(labels))),
            )

    def remove_issue_label(self, *, repo: str, issue_number: int, label: str) -> None:
        with self.lock:
            issue = self._matching_issue(repo, issue_number)
            self.issue = GitHubIssue(
                repo=issue.repo,
                number=issue.number,
                url=issue.url,
                title=issue.title,
                labels=tuple(item for item in issue.labels if item != label),
            )

    def comment_issue(self, *, repo: str, issue_number: int, body: str) -> None:
        self._matching_issue(repo, issue_number)

    def open_pull_request_exists(self, *, repo: str, branch: str) -> bool:
        return False

    def repository_dispatch(
        self,
        *,
        repo: str,
        event_type: str,
        client_payload: dict[str, Any],
    ) -> None:
        if len(client_payload) > 10:
            raise ValueError("GitHub repository_dispatch client_payload allows at most 10 top-level properties")
        action = {
            "action": "repository_dispatch",
            "repo": repo,
            "event_type": event_type,
            "client_payload": client_payload,
        }
        with self.lock:
            self.dispatches.append(action)
            write_json_file(self.dispatches_path, self.dispatches)

    def _matching_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        if self.issue is None or self.issue.repo != repo or self.issue.number != issue_number:
            raise VerificationError(f"mock GitHub issue {repo}#{issue_number} does not exist")
        return self.issue

    def _consume_approval_signal(self) -> None:
        if self.approval_consumed or self.issue is None or not self.approval_signal_path.exists():
            return
        approval = read_json_file(self.approval_signal_path)
        if approval != {"issue_number": self.issue.number, "repo": self.issue.repo}:
            raise VerificationError("mock GitHub approval signal does not match the created issue")
        self.approval_signal_path.unlink()
        self.approval_consumed = True
        self.issue = GitHubIssue(
            repo=self.issue.repo,
            number=self.issue.number,
            url=self.issue.url,
            title=self.issue.title,
            labels=tuple(sorted(set(self.issue.labels).union({AUTOFIX_APPROVED_LABEL}))),
        )


def sample_incident() -> dict[str, Any]:
    return {
        "version": 1,
        "source": "homelab-log-watcher",
        "detected_at": "2026-11-02T04:26:00+00:00",
        "incident": {
            "container_id": "verification-container-id",
            "container_name": "sample-service",
            "image": "ghcr.io/example/sample-service:verification",
            "severity": "ERROR",
            "matched_pattern": "database connection failed",
            "line": "ERROR database connection failed",
            "normalized_line": "ERROR database connection failed",
            "fingerprint": "verification-incident-fingerprint",
            "occurred_at": "2026-11-02T04:26:00+00:00",
        },
    }


def make_config(run_dir: Path, incident_token: str) -> Config:
    return Config(
        state_path=run_dir / "state.sqlite3",
        service_metadata_path=run_dir / "services.yaml",
        diagnostic_dir=run_dir / "diagnostics",
        diagnostic_reference_root="/verification/diagnostics",
        incident_token=incident_token,
        github_token=None,
        github_auth_mode="token",
        github_app_id=None,
        github_app_installation_id=None,
        github_app_private_key=None,
        github_api_url="https://api.github.invalid",
        default_issue_repo="example/homelab-config",
        dry_run=False,
        docker_log_tail=200,
        docker_log_lookback_seconds=600,
        episode_window_seconds=0,
        diagnostic_max_bytes=1_000_000,
        investigation_cooldown_seconds=0,
        issue_comment_cooldown_seconds=3600,
        operational_notification_cooldown_seconds=3600,
        codex_global_daily_limit=3,
        approval_poll_seconds=1,
        issue_notifications_enabled=False,
        phone_approvals_enabled=False,
        diagnostic_publish_enabled=True,
        diagnostic_s3_bucket=None,
        diagnostic_s3_region=None,
        diagnostic_s3_prefix="verification",
        diagnostic_url_ttl_seconds=3600,
        diagnostic_retention_days=14,
        service_host="127.0.0.1",
        service_port=0,
        http_timeout_seconds=5,
        log_level="INFO",
    )


def serve(run_dir: Path) -> int:
    incident_token = os.environ.get("VERIFY_INCIDENT_TOKEN")
    if not incident_token:
        raise VerificationError("verification child did not receive its incident token")
    config = make_config(run_dir, incident_token)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    github = VerificationGitHub(
        run_dir / "dispatches.json",
        run_dir / "approve-issue.json",
    )
    service = SREService(
        config=config,
        catalog=load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo),
        state=StateStore(config.state_path),
        github=github,
        logs=VerificationLogs(),
        now_func=lambda: FIXED_NOW,
        diagnostic_publisher=VerificationDiagnosticPublisher(),
    )
    server = SREServer(config=config, service=service)
    poller_stopped = threading.Event()
    server_stopped = threading.Event()

    def stop_server(signum: int, frame: Any) -> None:
        server_stopped.set()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    server_thread = threading.Thread(target=server.serve_forever, name="verification-http-server")
    server.thread = server_thread
    server_thread.start()
    deadline = time.monotonic() + 5
    while server.httpd is None and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if server.httpd is None:
        raise VerificationError("SREServer did not bind its HTTP socket")
    port = server.httpd.server_address[1]
    write_json_file(
        run_dir / "server.json",
        {"pid": os.getpid(), "url": f"http://{config.service_host}:{port}"},
    )

    def poll_approvals() -> None:
        while not poller_stopped.wait(0.05):
            try:
                service.poll_autofix_approvals(now=FIXED_NOW)
            except Exception:
                logging.getLogger("homelab-sre-agent.verification").exception(
                    "Autofix approval poll failed"
                )

    poller = threading.Thread(target=poll_approvals, name="verification-approval-poller", daemon=True)
    poller.start()

    try:
        while not server_stopped.wait(0.05):
            if not server_thread.is_alive():
                raise VerificationError("SREServer stopped before cleanup")
    finally:
        poller_stopped.set()
        server.httpd.shutdown()
        server_thread.join(timeout=5)
        server.httpd.server_close()
        poller.join(timeout=1)
    return 0


def launch_instance(evidence_root: Path) -> tuple[dict[str, Any], subprocess.Popen[bytes]]:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(tempfile.mkdtemp(prefix=f"homelab-sre-agent-verification-{run_id}-"))
    evidence_dir = evidence_root / run_id
    log_path = run_dir / "server.log"
    process: subprocess.Popen[bytes] | None = None
    process_identity: dict[str, str] | None = None
    try:
        evidence_dir.mkdir(parents=True, exist_ok=False)
        write_metadata(run_dir / "services.yaml")
        incident_token = secrets.token_urlsafe(32)
        token_path = run_dir / "incident-token"
        token_path.write_text(incident_token, encoding="utf-8")
        token_path.chmod(0o600)
        environment = os.environ.copy()
        environment["VERIFY_INCIDENT_TOKEN"] = incident_token
        with log_path.open("ab", buffering=0) as log_file:
            process = subprocess.Popen(
                [sys.executable, str(HELPER_PATH), "serve", "--run-dir", str(run_dir)],
                cwd=REPO_ROOT,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        process_identity = read_process_identity(process.pid)
        server = wait_for_json(run_dir / "server.json", process=process, timeout_seconds=10)
        runtime_path = run_dir / "runtime.json"
        runtime = {
            "run_id": run_id,
            "pid": process.pid,
            "process_identity": process_identity,
            "url": server["url"],
            "run_dir": str(run_dir),
            "runtime_file": str(runtime_path),
            "token_file": str(token_path),
            "approval_signal_file": str(run_dir / "approve-issue.json"),
            "dispatches_file": str(run_dir / "dispatches.json"),
            "server_log": str(log_path),
            "evidence_dir": str(evidence_dir),
        }
        write_json_file(runtime_path, runtime)
        return runtime, process
    except Exception as exc:
        cleanup_error: Exception | None = None
        if process is not None:
            try:
                if process_identity is None and process.poll() is None:
                    process_identity = read_process_identity(process.pid)
                if process_identity is not None:
                    stop_owned_process(process, process_identity, run_dir)
            except Exception as stop_exc:
                cleanup_error = stop_exc
        try:
            copy_server_log(log_path, evidence_dir)
        except Exception as copy_exc:
            cleanup_error = cleanup_error or copy_exc
        shutil.rmtree(run_dir, ignore_errors=True)
        failure_results = {
            "failure": f"launch failed: {type(exc).__name__}",
            "cleanup": {
                "process_stopped": process is None or process.poll() is not None,
                "scratch_removed": not run_dir.exists(),
                "evidence_retained": True,
            },
        }
        try:
            write_evidence(failure_results, evidence_dir)
        except Exception as evidence_exc:
            cleanup_error = cleanup_error or evidence_exc
        if cleanup_error is not None:
            raise VerificationError(f"launch cleanup failed: {cleanup_error}") from exc
        raise


def doctor_instance(runtime: dict[str, Any]) -> dict[str, Any]:
    verify_owned_process(runtime)
    status, body = request_json("GET", f"{runtime['url']}/health")
    require(status == 200, f"health returned HTTP {status}")
    require(body == {"dry_run": False, "ok": True}, f"unexpected health response {body!r}")
    return {"status": status, "body": body, "process_identity_verified": True}


def drive_instance(runtime: dict[str, Any]) -> dict[str, Any]:
    incident = sample_incident()
    require(
        set(incident) == set(ENVELOPE_PROPERTIES).union({"incident"}),
        "sample incident envelope does not match the contract",
    )
    require(
        set(incident["incident"]) == set(INCIDENT_PROPERTIES),
        "sample incident body does not match the contract",
    )
    intake_logical_properties = len(ENVELOPE_PROPERTIES) + len(incident["incident"])
    require(intake_logical_properties == 12, "sample incident must have 12 logical properties")

    unauthorized_status, unauthorized_body = request_json(
        "POST",
        f"{runtime['url']}/v1/incidents",
        payload=incident,
    )
    require(unauthorized_status == 401, f"incident intake without a token returned HTTP {unauthorized_status}")
    require(unauthorized_body == {"error": "unauthorized"}, "unauthorized response body changed")

    token = Path(runtime["token_file"]).read_text(encoding="utf-8")
    intake_status, intake_body = request_json(
        "POST",
        f"{runtime['url']}/v1/incidents",
        payload=incident,
        token=token,
    )
    require(intake_status == 200, f"incident intake returned HTTP {intake_status}")
    require(intake_body.get("ok") is True, f"incident intake failed with {intake_body!r}")
    require(intake_body.get("status") == "created", f"unexpected intake result {intake_body!r}")

    issue = intake_body.get("issue")
    require(isinstance(issue, dict), "incident intake did not return an issue")
    write_json_file(
        Path(runtime["approval_signal_file"]),
        {"issue_number": issue["number"], "repo": issue["repo"]},
    )

    dispatches_path = Path(runtime["dispatches_file"])
    wait_for_json(dispatches_path, timeout_seconds=10, predicate=lambda value: bool(value))
    time.sleep(0.2)
    dispatches = read_json_file(dispatches_path)
    require(isinstance(dispatches, list), "captured dispatch evidence is not a list")
    require(len(dispatches) == 1, f"expected exactly one repository dispatch, found {len(dispatches)}")
    require(
        not Path(runtime["approval_signal_file"]).exists(),
        "mock GitHub did not consume the one-shot approval signal",
    )
    dispatch = dispatches[0]
    require(dispatch.get("action") == "repository_dispatch", "captured GitHub action is not a dispatch")
    require(dispatch.get("repo") == "example/sample-service", "dispatch targeted the wrong repository")
    require(dispatch.get("event_type") == "homelab-sre-investigate", "dispatch used the wrong event type")

    client_payload = dispatch.get("client_payload")
    require(isinstance(client_payload, dict), "dispatch client_payload is not an object")
    require(
        set(client_payload) == set(DISPATCH_BASE_PROPERTIES).union({"diagnostic"}),
        "dispatch base properties do not match the contract",
    )
    diagnostic = client_payload["diagnostic"]
    require(isinstance(diagnostic, dict), "dispatch diagnostic is not an object")
    require(
        set(diagnostic) == set(DIAGNOSTIC_PROPERTIES),
        "dispatch diagnostic properties do not match the contract",
    )
    dispatch_logical_properties = len(DISPATCH_BASE_PROPERTIES) + len(diagnostic)
    dispatch_top_level_properties = len(client_payload)
    require(dispatch_logical_properties == 12, "dispatch must have 12 logical properties")
    require(dispatch_top_level_properties <= 10, "dispatch exceeds GitHub's top-level property limit")

    malformed_status, malformed_body = request_json(
        "POST",
        f"{runtime['url']}/v1/incidents",
        raw_body=b'{"incident":',
        token=token,
    )
    require(malformed_status == 422, f"malformed incident returned HTTP {malformed_status}")
    error = malformed_body.get("error")
    require(isinstance(error, str) and bool(error.strip()), "malformed incident response has no useful error")

    return {
        "auth": {
            "unauthorized_status": unauthorized_status,
            "bearer_status": intake_status,
            "token_printed": False,
        },
        "incident_request": {
            "payload": incident,
            "envelope_properties": len(ENVELOPE_PROPERTIES),
            "incident_properties": len(incident["incident"]),
            "logical_properties": intake_logical_properties,
        },
        "intake_response": {"status": intake_status, "body": intake_body},
        "approval": {"signaled_after_intake": True, "one_shot": True},
        "repository_dispatches": dispatches,
        "dispatch_properties": {
            "base": len(DISPATCH_BASE_PROPERTIES),
            "diagnostic": len(diagnostic),
            "logical": dispatch_logical_properties,
            "top_level": dispatch_top_level_properties,
            "top_level_limit": 10,
        },
        "malformed_response": {"status": malformed_status, "body": malformed_body},
    }


def cleanup_instance(
    runtime: dict[str, Any],
    *,
    process: subprocess.Popen[bytes] | None,
    results: dict[str, Any],
) -> None:
    pid = int(runtime["pid"])
    run_dir = Path(runtime["run_dir"])
    process_identity = runtime["process_identity"]
    require(isinstance(process_identity, dict), "runtime process identity is missing")
    if process is not None:
        require(process.pid == pid, "owned child process does not match the runtime file")
        stop_owned_process(process, process_identity, run_dir)
        process_stopped = process.poll() is not None
    else:
        actual_identity = try_process_identity(pid)
        if actual_identity is not None:
            stop_runtime_process(pid, process_identity, run_dir)
        process_stopped = not process_identity_is_running(pid, process_identity)
    require(process_stopped, f"verification process {pid} is still running")
    copy_server_log(Path(runtime["server_log"]), Path(runtime["evidence_dir"]))
    shutil.rmtree(run_dir)
    scratch_removed = not run_dir.exists()
    require(scratch_removed, f"scratch directory still exists at {run_dir}")
    results["cleanup"] = {
        "process_stopped": process_stopped,
        "scratch_removed": scratch_removed,
        "evidence_retained": True,
    }
    write_evidence(results, Path(runtime["evidence_dir"]))


def run_verification(evidence_root: Path) -> int:
    runtime: dict[str, Any] | None = None
    process: subprocess.Popen[bytes] | None = None
    results: dict[str, Any] = {}
    failed = False
    try:
        runtime, process = launch_instance(evidence_root)
        results["launch"] = {
            "pid": runtime["pid"],
            "process_identity": runtime["process_identity"],
            "url": runtime["url"],
            "scratch": runtime["run_dir"],
        }
        results["doctor"] = doctor_instance(runtime)
        results.update(drive_instance(runtime))
    except Exception as exc:
        failed = True
        results["failure"] = str(exc)
    finally:
        if runtime is not None:
            try:
                cleanup_instance(runtime, process=process, results=results)
            except Exception as exc:
                failed = True
                results["failure"] = str(exc)
                write_evidence(results, Path(runtime["evidence_dir"]))
    if runtime is None:
        raise VerificationError(results.get("failure", "verification launch failed"))
    transcript = (Path(runtime["evidence_dir"]) / "transcript.txt").read_text(encoding="utf-8")
    print(transcript, end="")
    return 1 if failed else 0


def write_metadata(path: Path) -> None:
    path.write_text(
        """defaults:
  issue_repo: example/homelab-config
  deploy_repo: example/homelab-config
services:
  sample-service:
    containers: [sample-service]
    images: ["ghcr.io/example/sample-service:*"]
    source:
      repo: example/sample-service
    issue_repo: example/sample-service
    deploy:
      repo: example/homelab-config
      path: services/sample-service
    labels: [homelab-sre, sample-service]
    sre:
      enabled: true
      autofix: true
      repo_daily_limit: 5
""",
        encoding="utf-8",
    )


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = raw_body
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            response_body = response.read()
    except HTTPError as exc:
        status = exc.code
        response_body = exc.read()
    try:
        parsed = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{method} {url} returned a non-JSON body") from exc
    require(isinstance(parsed, dict), f"{method} {url} returned a non-object JSON body")
    return status, parsed


def wait_for_json(
    path: Path,
    *,
    timeout_seconds: float,
    process: subprocess.Popen[bytes] | None = None,
    predicate: Any = None,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise VerificationError(f"verification child exited with code {process.returncode}")
        if path.exists():
            try:
                value = read_json_file(path)
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc
            else:
                if predicate is None or predicate(value):
                    return value
        time.sleep(0.05)
    detail = f": {last_error}" if last_error is not None else ""
    raise VerificationError(f"timed out waiting for {path}{detail}")


def write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stop_owned_process(
    process: subprocess.Popen[bytes],
    process_identity: dict[str, str],
    run_dir: Path,
) -> None:
    if process.poll() is not None:
        return
    verify_process_identity(process.pid, process_identity, run_dir)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        verify_process_identity(process.pid, process_identity, run_dir)
        process.kill()
        process.wait(timeout=5)


def stop_runtime_process(
    pid: int,
    process_identity: dict[str, str],
    run_dir: Path,
) -> None:
    verify_process_identity(pid, process_identity, run_dir)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_identity_is_running(pid, process_identity):
            return
        time.sleep(0.05)
    verify_process_identity(pid, process_identity, run_dir)
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_identity_is_running(pid, process_identity):
            return
        time.sleep(0.05)
    raise VerificationError(f"verification process {pid} did not stop")


def read_process_identity(pid: int) -> dict[str, str]:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    command_end = stat.rfind(")")
    if command_end < 0:
        raise VerificationError(f"could not parse process identity for PID {pid}")
    fields = stat[command_end + 2 :].split()
    if len(fields) <= 19:
        raise VerificationError(f"process identity for PID {pid} is incomplete")
    return {"boot_id": boot_id, "start_time_ticks": fields[19]}


def try_process_identity(pid: int) -> dict[str, str] | None:
    try:
        return read_process_identity(pid)
    except FileNotFoundError:
        return None


def process_identity_is_running(pid: int, expected: dict[str, str]) -> bool:
    return try_process_identity(pid) == expected


def verify_process_identity(pid: int, expected: dict[str, str], run_dir: Path) -> None:
    actual = try_process_identity(pid)
    require(actual is not None, f"verification process {pid} is not running")
    require(actual == expected, f"verification process {pid} start identity changed")
    require(process_matches_runtime(pid, run_dir), f"process {pid} is not the verification helper")


def verify_owned_process(runtime: dict[str, Any]) -> None:
    pid = int(runtime["pid"])
    process_identity = runtime.get("process_identity")
    require(isinstance(process_identity, dict), "runtime process identity is missing")
    verify_process_identity(pid, process_identity, Path(runtime["run_dir"]))


def process_matches_runtime(pid: int, run_dir: Path) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    arguments = {item.decode("utf-8", errors="replace") for item in command if item}
    return str(HELPER_PATH) in arguments and "serve" in arguments and str(run_dir) in arguments


def copy_server_log(log_path: Path, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / "server.log"
    if log_path.exists():
        shutil.copyfile(log_path, target)
    else:
        target.write_text("", encoding="utf-8")


def write_evidence(results: dict[str, Any], evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    write_json_file(evidence_dir / "results.json", results)
    (evidence_dir / "transcript.txt").write_text(
        render_transcript(results, evidence_dir),
        encoding="utf-8",
    )


def render_transcript(results: dict[str, Any], evidence_dir: Path) -> str:
    lines = ["homelab-sre-agent HTTP verification"]
    if "failure" in results:
        lines.append(f"verification: fail {results['failure']}")
    if "doctor" in results:
        health = results["doctor"]
        lines.append(
            f"doctor: pass owned process, HTTP {health['status']}, ok=true, dry_run=false"
        )
    if "auth" in results:
        auth = results["auth"]
        lines.append(
            "bearer auth: pass "
            f"unauthorized={auth['unauthorized_status']}, authorized={auth['bearer_status']}, token not printed"
        )
    if "incident_request" in results:
        shape = results["incident_request"]
        lines.append(
            "incident sample: pass "
            f"{shape['logical_properties']} logical properties "
            f"({shape['envelope_properties']} envelope + {shape['incident_properties']} incident)"
        )
    if "intake_response" in results:
        intake = results["intake_response"]
        lines.append(
            "incident intake: pass "
            f"HTTP {intake['status']}, status={intake['body']['status']}, issue={intake['body']['issue']['number']}"
        )
    if "approval" in results:
        lines.append("approval signal: pass one-shot signal sent after HTTP 200 intake")
    if "repository_dispatches" in results:
        dispatch = results["repository_dispatches"][0]
        lines.append(
            "repository dispatch: pass "
            f"count=1, event={dispatch['event_type']}, repo={dispatch['repo']}"
        )
    if "dispatch_properties" in results:
        shape = results["dispatch_properties"]
        lines.append(
            "dispatch payload: pass "
            f"{shape['logical']} logical properties "
            f"({shape['base']} base + {shape['diagnostic']} diagnostic), "
            f"{shape['top_level']} top-level <= {shape['top_level_limit']}"
        )
    if "malformed_response" in results:
        malformed = results["malformed_response"]
        error = str(malformed["body"]["error"]).replace("\n", " ")
        lines.append(
            f"malformed incident: pass HTTP {malformed['status']}, error={json.dumps(error)}"
        )
    if "cleanup" in results:
        lines.append("process teardown: pass")
        lines.append("scratch cleanup: pass")
    lines.append(f"evidence retained: {evidence_dir}")
    return "\n".join(lines) + "\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def load_runtime(path: Path) -> dict[str, Any]:
    runtime = read_json_file(path)
    require(isinstance(runtime, dict), "runtime file is not a JSON object")
    return runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify homelab-sre-agent through its HTTP API.")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="launch, doctor, drive, capture evidence, and clean up")
    run_parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)

    launch_parser = commands.add_parser("launch", help="start an isolated verification instance")
    launch_parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)

    for name in ("doctor", "drive", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--runtime", type=Path, required=True)

    serve_parser = commands.add_parser("serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "serve":
            return serve(args.run_dir)
        if args.command == "run":
            return run_verification(args.evidence_root)
        if args.command == "launch":
            runtime, process = launch_instance(args.evidence_root)
            print(f"runtime_file={runtime['runtime_file']}")
            print(f"service_url={runtime['url']}")
            print(f"evidence_dir={runtime['evidence_dir']}")
            return 0

        runtime = load_runtime(args.runtime)
        if args.command == "doctor":
            health = doctor_instance(runtime)
            print(f"doctor: pass owned process, HTTP {health['status']}, ok=true, dry_run=false")
            return 0
        if args.command == "drive":
            results_path = Path(runtime["evidence_dir"]) / "results.json"
            results = read_json_file(results_path) if results_path.exists() else {}
            results["doctor"] = doctor_instance(runtime)
            results.update(drive_instance(runtime))
            write_evidence(results, Path(runtime["evidence_dir"]))
            print((Path(runtime["evidence_dir"]) / "transcript.txt").read_text(encoding="utf-8"), end="")
            return 0
        if args.command == "cleanup":
            results_path = Path(runtime["evidence_dir"]) / "results.json"
            results = read_json_file(results_path) if results_path.exists() else {}
            cleanup_instance(runtime, process=None, results=results)
            print((Path(runtime["evidence_dir"]) / "transcript.txt").read_text(encoding="utf-8"), end="")
            return 0
    except (OSError, VerificationError, ValueError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    raise VerificationError(f"unsupported command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
