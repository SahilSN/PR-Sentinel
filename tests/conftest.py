import json
from pathlib import Path

import httpx
import pytest

from pr_agent.github_client import API, GitHubClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeGitHub:
    """In-memory GitHub REST API backed by a fixture, served through httpx.MockTransport."""

    def __init__(self, fixture: dict, bot_login: str = "github-actions[bot]"):
        self.fixture = fixture
        self.bot_login = bot_login
        self.comments: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.next_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        repo, pr = self.fixture["repo"], self.fixture["pr_number"]
        path = request.url.path
        if request.method == "GET" and path == f"/repos/{repo}/pulls/{pr}":
            return httpx.Response(200, json={
                "title": self.fixture["title"],
                "base": {"sha": self.fixture["base_sha"]},
                "head": {"sha": self.fixture["head_sha"]},
            })
        if request.method == "GET" and path == f"/repos/{repo}/pulls/{pr}/files":
            return httpx.Response(200, json=[
                {"filename": f["path"], "status": f["status"], "patch": f["patch"],
                 "additions": f["additions"], "deletions": f["deletions"]}
                for f in self.fixture["changed_files"]
            ])
        if request.method == "GET" and path.startswith(f"/repos/{repo}/contents/"):
            file_path = path.removeprefix(f"/repos/{repo}/contents/")
            content = self.fixture.get("file_contents", {}).get(file_path)
            if content is None or request.url.params.get("ref") != self.fixture["head_sha"]:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, content=content.encode())
        ci = self.fixture.get("ci") or {}
        if request.method == "GET" and path == f"/repos/{repo}/commits/{self.fixture['head_sha']}/check-runs":
            if ci.get("error"):
                return httpx.Response(ci["error"]["status"], json={"message": ci["error"]["message"]})
            per_page, page = int(request.url.params["per_page"]), int(request.url.params["page"])
            runs = ci.get("check_runs", [])
            return httpx.Response(200, json={"total_count": ci.get("total_count", len(runs)),
                                             "check_runs": runs[(page - 1) * per_page: page * per_page]})
        if request.method == "GET" and path.startswith(f"/repos/{repo}/check-runs/") and path.endswith("/annotations"):
            run_id = path.split("/")[-2]
            return httpx.Response(200, json=(ci.get("annotations") or {}).get(run_id, []))
        if request.method == "GET" and path == f"/repos/{repo}/git/trees/{self.fixture['head_sha']}":
            contents = self.fixture.get("file_contents", {})
            tree = [{"path": p, "type": "blob", "mode": "100644", "size": len(c)} for p, c in contents.items()]
            tree += self.fixture.get("extra_tree_entries", [])
            return httpx.Response(200, json={"tree": tree, "truncated": self.fixture.get("tree_truncated", False)})
        if request.method == "GET" and path == "/user":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path == f"/repos/{repo}/issues/{pr}/comments":
            if request.method == "GET":
                return httpx.Response(200, json=self.comments)
            body = json.loads(request.content)["body"]
            comment = {"id": self.next_id, "body": body, "user": {"login": self.bot_login}}
            self.next_id += 1
            self.comments.append(comment)
            return httpx.Response(201, json=comment)
        if request.method == "PATCH" and path.startswith(f"/repos/{repo}/issues/comments/"):
            cid = int(path.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=c)
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": f"unhandled {request.method} {path}"})

    def client(self) -> GitHubClient:
        http = httpx.Client(base_url=API, transport=httpx.MockTransport(self.handler))
        return GitHubClient("ghp_TESTTOKEN_should_never_leak", http=http, sleep=lambda s: None)

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_MODEL", "MAX_AGENT_STEPS",
                 "PR_SENTINEL_BOT_LOGIN"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
