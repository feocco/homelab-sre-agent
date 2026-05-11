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


@dataclass(frozen=True)
class GitHubIssue:
    repo: str
    number: int
    url: str
    title: str
    labels: tuple[str, ...]


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

    def find_open_issue_by_title(self, *, repo: str, title: str, label: str | None = None) -> IssueResult | None:
        if self.dry_run:
            return None
        query = f"/repos/{repo_path(repo)}/issues?state=open&per_page=100"
        if label:
            query += f"&labels={quote(label, safe='')}"
        response = self._request("GET", query, None)
        if not isinstance(response, list):
            raise RuntimeError("GitHub API returned a non-list issue response")
        for item in response:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            if str(item.get("title") or "") == title:
                return IssueResult(repo=repo, number=int(item["number"]), url=str(item["html_url"]))
        return None

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

    def list_open_issues_with_label(self, *, repo: str, label: str) -> list[GitHubIssue]:
        if self.dry_run:
            return []
        response = self._request(
            "GET",
            f"/repos/{repo_path(repo)}/issues?state=open&labels={quote(label, safe='')}&per_page=100",
            None,
        )
        if not isinstance(response, list):
            raise RuntimeError("GitHub API returned a non-list issue response")
        issues: list[GitHubIssue] = []
        for item in response:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            labels = tuple(
                str(label_item.get("name"))
                for label_item in item.get("labels", [])
                if isinstance(label_item, dict) and label_item.get("name")
            )
            issues.append(
                GitHubIssue(
                    repo=repo,
                    number=int(item["number"]),
                    url=str(item["html_url"]),
                    title=str(item.get("title") or ""),
                    labels=labels,
                )
            )
        return issues

    def add_issue_labels(self, *, repo: str, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        if self.dry_run:
            self.dry_run_actions.append(
                {"action": "add_issue_labels", "repo": repo, "issue_number": issue_number, "labels": labels}
            )
            return
        self._request("POST", f"/repos/{repo_path(repo)}/issues/{issue_number}/labels", {"labels": labels})

    def remove_issue_label(self, *, repo: str, issue_number: int, label: str) -> None:
        if self.dry_run:
            self.dry_run_actions.append(
                {"action": "remove_issue_label", "repo": repo, "issue_number": issue_number, "label": label}
            )
            return
        self._request(
            "DELETE",
            f"/repos/{repo_path(repo)}/issues/{issue_number}/labels/{quote(label, safe='')}",
            None,
            expect_json=False,
            not_found_ok=True,
        )

    def ensure_label(self, *, repo: str, name: str, color: str, description: str) -> None:
        if self.dry_run:
            return
        existing = self._request(
            "GET",
            f"/repos/{repo_path(repo)}/labels/{quote(name, safe='')}",
            None,
            not_found_ok=True,
        )
        if existing is not None:
            return
        self._request(
            "POST",
            f"/repos/{repo_path(repo)}/labels",
            {"name": name, "color": color, "description": description},
        )

    def open_pull_request_exists(self, *, repo: str, branch: str) -> bool:
        if self.dry_run:
            return False
        owner, _ = repo.split("/", 1)
        head = quote(f"{owner}:{branch}", safe="")
        response = self._request(
            "GET",
            f"/repos/{repo_path(repo)}/pulls?state=open&head={head}&per_page=1",
            None,
        )
        if not isinstance(response, list):
            raise RuntimeError("GitHub API returned a non-list pull request response")
        return bool(response)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        expect_json: bool = True,
        not_found_ok: bool = False,
    ) -> Any:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required when SRE_DRY_RUN=false")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "homelab-sre-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
        except HTTPError as exc:
            if not_found_ok and exc.code == 404:
                return None
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed: {exc.code} {details}") from exc
        if response.status == 204:
            return {}
        if not expect_json or not data:
            return {}
        decoded = json.loads(data.decode("utf-8"))
        return decoded


def repo_path(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
