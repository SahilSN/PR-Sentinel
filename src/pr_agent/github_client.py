"""Small GitHub REST wrapper: read PR data, read files at a pinned SHA, create/update one issue comment."""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from .schemas import ChangedFile, PullRequestInfo

API = "https://api.github.com"
MAX_PAGES = 30  # 30 x 100 = GitHub's own 3000-file cap for PR file listings
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GitHubError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"GitHub API error ({status}): {message}" if status else f"GitHub API error: {message}")
        self.status = status


class GitHubClient:
    def __init__(self, token: str, http: httpx.Client | None = None, max_retries: int = 3, sleep=time.sleep):
        self._http = http or httpx.Client(base_url=API, timeout=20.0)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-sentinel",
        }
        self._max_retries = max_retries
        self._sleep = sleep
        self.write_requests = 0  # counted so tests can prove dry-run never writes

    # ---------- HTTP plumbing ----------

    def _request(self, method: str, url: str, *, accept: str | None = None, **kwargs) -> httpx.Response:
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        if method != "GET":
            self.write_requests += 1
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                # Message only: exception text never includes our request headers.
                if attempt == self._max_retries:
                    raise GitHubError(None, f"network error: {type(exc).__name__}") from None
                self._sleep(2**attempt)
                continue
            rate_limited = resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"
            if (resp.status_code in RETRY_STATUSES or rate_limited) and attempt < self._max_retries:
                retry_after = resp.headers.get("retry-after")
                self._sleep(min(float(retry_after), 10.0) if retry_after and retry_after.isdigit() else 2**attempt)
                continue
            if resp.status_code >= 400:
                try:
                    message = resp.json().get("message", "")
                except ValueError:
                    message = resp.text[:200]
                raise GitHubError(resp.status_code, message)
            return resp
        raise GitHubError(None, "retries exhausted")  # unreachable, keeps type checkers happy

    def _paginate(self, url: str, max_pages: int = MAX_PAGES) -> list[dict]:
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request("GET", url, params={"per_page": 100, "page": page}).json()
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    # ---------- Reads ----------

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestInfo:
        data = self._request("GET", f"/repos/{repo}/pulls/{int(pr_number)}").json()
        return PullRequestInfo(
            repo=repo,
            pr_number=pr_number,
            title=data.get("title") or "",
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
        )

    def get_changed_files(self, repo: str, pr_number: int) -> list[ChangedFile]:
        return [
            ChangedFile(
                path=f["filename"],
                status=f.get("status", "modified"),
                patch=f.get("patch"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
            )
            for f in self._paginate(f"/repos/{repo}/pulls/{int(pr_number)}/files")
        ]

    def read_file(self, repo: str, path: str, ref: str) -> str:
        """Raw text of `path` at commit `ref`. Callers must pass the validated head SHA and a vetted path."""
        resp = self._request(
            "GET",
            f"/repos/{repo}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            accept="application/vnd.github.raw+json",
        )
        raw = resp.content
        if b"\x00" in raw[:8192]:
            raise GitHubError(None, f"{path} looks like a binary file")
        return raw.decode("utf-8", errors="replace")

    def list_issue_comments(self, repo: str, pr_number: int) -> list[dict]:
        return self._paginate(f"/repos/{repo}/issues/{int(pr_number)}/comments", max_pages=10)

    def get_authenticated_login(self) -> str | None:
        """Login behind the token, or None (the Actions GITHUB_TOKEN cannot call /user)."""
        try:
            return self._request("GET", "/user").json().get("login")
        except GitHubError:
            return None

    # ---------- Writes (only reached with --publish) ----------

    def create_comment(self, repo: str, pr_number: int, body: str) -> int:
        resp = self._request("POST", f"/repos/{repo}/issues/{int(pr_number)}/comments", json={"body": body})
        return resp.json()["id"]

    def update_comment(self, repo: str, comment_id: int, body: str) -> int:
        resp = self._request("PATCH", f"/repos/{repo}/issues/comments/{int(comment_id)}", json={"body": body})
        return resp.json()["id"]
