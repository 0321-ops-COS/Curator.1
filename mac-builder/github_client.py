"""GitHub for the merge gates: pull requests, checks and merging."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

TOKEN_ENV = "MANAGER_GITHUB_TOKEN"
API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 20.0
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
STATUS_MESSAGES = {
    401: "GitHub rejected the token (401)", 403: "GitHub refused it (403): no access",
    404: "GitHub has nothing at that path (404)", 405: "GitHub does not allow that (405)",
    409: "the branch head changed; re-check it and try again",
    422: "GitHub could not merge this (422): it is not mergeable",
}
MERGEABLE_RETRIES = 3
MERGEABLE_PAUSE_SECONDS = 2.0


class GitHubError(Exception):
    """A GitHub call that did not succeed. Never carries the token or headers."""


def _mask(text: str, token: str | None) -> str:
    return text.replace(token, "[redacted]") if token else text


def revert_hint(owner: str, repo: str, number: int) -> str:
    return f"https://github.com/{owner}/{repo}/pull/{number}: press Revert on that page"


def _check_state(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    passed = run.get("conclusion") in ("success", "neutral", "skipped")
    return "success" if passed else "failure"


def _status_state(state: object) -> str:
    return str(state) if str(state) in ("success", "failure", "pending", "error") else "pending"


class GitHubClient:
    def __init__(self, owner: str, repo: str, token: str | None, transport=None) -> None:
        self.owner, self.repo, self.token = owner, repo, token
        self._client = httpx.Client(
            base_url=API_ROOT, timeout=httpx.Timeout(TIMEOUT_SECONDS),
            transport=transport, headers=dict(HEADERS),
        )

    @classmethod
    def from_env(cls, owner: str, repo: str) -> "GitHubClient":
        return cls(owner, repo, os.environ.get(TOKEN_ENV, "").strip() or None)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.token:
            raise GitHubError(f"{TOKEN_ENV} is not set")
        auth = {"Authorization": f"Bearer {self.token}"}
        try:
            response = self._client.request(method, path, headers=auth, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(_mask(f"GitHub {type(exc).__name__}", self.token)) from None
        if response.status_code >= 400:
            message = STATUS_MESSAGES.get(response.status_code, f"GitHub {response.status_code}")
            raise GitHubError(_mask(message, self.token))
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise GitHubError("GitHub returned a body that is not JSON") from None

    def find_pr(self, branch: str, base: str) -> dict | None:
        params = {"head": f"{self.owner}:{branch}", "base": base, "state": "open"}
        pulls = self._request("GET", f"/repos/{self.owner}/{self.repo}/pulls", params=params)
        return pulls[0] if pulls else None

    def open_pr(self, branch: str, base: str, title: str, body: str) -> dict:
        payload = {"title": title, "body": body, "head": branch, "base": base}
        return self._request("POST", f"/repos/{self.owner}/{self.repo}/pulls", json=payload)

    def get_pr(self, number: int) -> dict:
        pull: dict = {}
        for attempt in range(MERGEABLE_RETRIES + 1):
            pull = self._request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{number}")
            if pull.get("mergeable") is not None or attempt == MERGEABLE_RETRIES:
                break
            time.sleep(MERGEABLE_PAUSE_SECONDS)
        return {"mergeable": pull.get("mergeable"),
                "mergeable_state": pull.get("mergeable_state"),
                "head_sha": str((pull.get("head") or {}).get("sha") or "")}

    def ci_states(self, sha: str) -> tuple[str, ...]:
        root = f"/repos/{self.owner}/{self.repo}/commits/{sha}"
        runs = self._request("GET", f"{root}/check-runs")
        states = [_check_state(run) for run in runs.get("check_runs") or ()]
        status = self._request("GET", f"{root}/status")
        if status.get("statuses"):
            states.append(_status_state(status.get("state")))
        return tuple(states)

    def merge_pr(self, number: int, expected_sha: str) -> dict:
        payload = {"merge_method": "merge", "sha": expected_sha}
        path = f"/repos/{self.owner}/{self.repo}/pulls/{number}/merge"
        return self._request("PUT", path, json=payload)
