from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class IssueResult:
    repo: str
    number: int
    url: str


class GitHubClient:
    def __init__(self, *, token: str | None, api_url: str, dry_run: bool, timeout_seconds: float) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.dry_run_actions: list[dict[str, Any]] = []

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str]) -> IssueResult:
        if self.dry_run:
            self.dry_run_actions.append(
                {"action": "create_issue", "repo": repo, "title": title, "body": body, "labels": labels}
            )
            return IssueResult(repo=repo, number=0, url=f"https://github.com/{repo}/issues/dry-run")
        response = self._request(
            "POST",
            f"/repos/{repo_path(repo)}/issues",
            {"title": title, "body": body, "labels": labels},
        )
        return IssueResult(repo=repo, number=int(response["number"]), url=str(response["html_url"]))

    def comment_issue(self, *, repo: str, issue_number: int, body: str) -> None:
        if self.dry_run:
            self.dry_run_actions.append(
                {"action": "comment_issue", "repo": repo, "issue_number": issue_number, "body": body}
            )
            return
        self._request("POST", f"/repos/{repo_path(repo)}/issues/{issue_number}/comments", {"body": body})

    def repository_dispatch(self, *, repo: str, event_type: str, client_payload: dict[str, Any]) -> None:
        if self.dry_run:
            self.dry_run_actions.append(
                {
                    "action": "repository_dispatch",
                    "repo": repo,
                    "event_type": event_type,
                    "client_payload": client_payload,
                }
            )
            return
        self._request(
            "POST",
            f"/repos/{repo_path(repo)}/dispatches",
            {"event_type": event_type, "client_payload": client_payload},
            expect_json=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required when SRE_DRY_RUN=false")
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "homelab-sre-agent",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed: {exc.code} {details}") from exc
        if not expect_json or not data:
            return {}
        decoded = json.loads(data.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("GitHub API returned a non-object response")
        return decoded


def repo_path(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
