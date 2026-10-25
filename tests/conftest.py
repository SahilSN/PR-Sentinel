import json
from pathlib import Path

import httpx
import pytest
import yaml

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
        # Milestone 3 state
        state = fixture.get("pr_state") or {}
        self.labels: list[str] = list(state.get("labels", []))
        self.merged = False
        self.merge_status: int | None = None  # set to 405/409 to simulate merge failures
        self.ref_conflict = False  # simulate the branch moving during an autofix
        self.reviewers: list[str] = []
        self.dispatches: list[dict] = []
        self.trees: list[dict] = []
        self.commits_created: list[dict] = []
        self.refs: dict[str, str] = {}
        self.issues: list[dict] = list(fixture.get("open_issues", []))
        self.pulls_created: list[dict] = []
        self.reactions: list[int] = []
        self.created_labels: set[str] = set()

    def raw_pull(self) -> dict:
        f, st = self.fixture, self.fixture.get("pr_state") or {}
        return {
            "number": f["pr_number"], "title": st.get("title", f["title"]), "body": st.get("body", ""),
            "state": "closed" if self.merged else st.get("state", "open"), "merged": self.merged,
            "draft": st.get("draft", False), "user": {"login": st.get("author", "demo-dev")},
            "labels": [{"name": l} for l in self.labels],
            "base": {"sha": f["base_sha"], "ref": st.get("base_ref", "main")},
            "head": {"sha": f["head_sha"], "ref": st.get("head_ref", "feature"),
                     "repo": {"full_name": f["repo"] if st.get("same_repo", True) else "someone/fork"}},
            "mergeable": st.get("mergeable", True), "mergeable_state": st.get("mergeable_state", "clean"),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        repo, pr = self.fixture["repo"], self.fixture["pr_number"]
        path = request.url.path
        if request.method == "GET" and path == f"/repos/{repo}/pulls/{pr}":
            return httpx.Response(200, json=self.raw_pull())
        if request.method == "GET" and path == f"/repos/{repo}/pulls/{pr}/files":
            return httpx.Response(200, json=[
                {"filename": f["path"], "status": f["status"], "patch": f["patch"],
                 "additions": f["additions"], "deletions": f["deletions"]}
                for f in self.fixture["changed_files"]
            ])
        if request.method == "GET" and path.startswith(f"/repos/{repo}/contents/"):
            file_path = path.removeprefix(f"/repos/{repo}/contents/")
            ref = request.url.params.get("ref")
            if ref == "main" and file_path == ".github/pr-sentinel.yml":
                cfg = self.fixture.get("sentinel_config")
                if cfg is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, content=(cfg if isinstance(cfg, str) else yaml.safe_dump(cfg)).encode())
            source = self.fixture.get("base_file_contents", {}) if ref == self.fixture["base_sha"] else \
                self.fixture.get("file_contents", {}) if ref == self.fixture["head_sha"] else {}
            content = source.get(file_path)
            if content is None:
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
        m3 = self.m3(request, repo, pr, path)
        if m3 is not None:
            return m3
        return httpx.Response(404, json={"message": f"unhandled {request.method} {path}"})

    def m3(self, request: httpx.Request, repo: str, pr: int, path: str) -> httpx.Response | None:
        """Milestone 3 endpoints."""
        f, st, method = self.fixture, self.fixture.get("pr_state") or {}, request.method
        body = json.loads(request.content) if request.content else {}
        if method == "GET" and path == f"/repos/{repo}":
            return httpx.Response(200, json={"default_branch": "main"})
        if method == "GET" and path.startswith(f"/repos/{repo}/collaborators/") and path.endswith("/permission"):
            user = path.split("/")[-2]
            perms = {st.get("author", "demo-dev"): st.get("author_permission", "write"), **st.get("permissions", {})}
            return httpx.Response(200, json={"role_name": perms.get(user, "read")})
        if method == "GET" and path == f"/repos/{repo}/pulls/{pr}/reviews":
            return httpx.Response(200, json=st.get("reviews", []))
        if method == "GET" and path == f"/repos/{repo}/pulls/{pr}/commits":
            commits = st.get("commits") or [{"sha": f["head_sha"], "message": "change",
                                             "login": st.get("author", "demo-dev")}]
            return httpx.Response(200, json=[{"sha": c["sha"], "commit": {"message": c["message"]},
                                              "author": {"login": c["login"]}} for c in commits])
        if method == "GET" and path == "/search/issues":
            return httpx.Response(200, json={"total_count": st.get("merges_today", 0)})
        if method == "GET" and path == f"/repos/{repo}/issues":
            wanted = request.url.params.get("labels")
            return httpx.Response(200, json=[i for i in self.issues if wanted in i.get("labels", [])])
        if method == "POST" and path == f"/repos/{repo}/labels":
            if body["name"] in self.created_labels:
                return httpx.Response(422, json={"message": "already_exists"})
            self.created_labels.add(body["name"])
            return httpx.Response(201, json=body)
        if method == "POST" and path == f"/repos/{repo}/issues/{pr}/labels":
            self.labels += [l for l in body["labels"] if l not in self.labels]
            return httpx.Response(200, json=[{"name": l} for l in self.labels])
        if method == "POST" and path.startswith(f"/repos/{repo}/issues/") and path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": l} for l in body["labels"]])  # another issue/PR
        if method == "POST" and path.startswith(f"/repos/{repo}/issues/") and path.endswith("/comments"):
            comment = {"id": self.next_id, "body": body["body"], "user": {"login": self.bot_login}}
            self.next_id += 1
            self.comments.append(comment)
            return httpx.Response(201, json=comment)
        if method == "DELETE" and path.startswith(f"/repos/{repo}/issues/{pr}/labels/"):
            name = httpx.URL(path).path.rsplit("/", 1)[1].replace("%3A", ":")
            if name not in self.labels:
                return httpx.Response(404, json={"message": "Label does not exist"})
            self.labels.remove(name)
            return httpx.Response(200, json=[])
        if method == "POST" and path == f"/repos/{repo}/pulls/{pr}/requested_reviewers":
            self.reviewers += body["reviewers"]
            return httpx.Response(201, json={})
        if method == "GET" and path.startswith(f"/repos/{repo}/git/ref/heads/"):
            return httpx.Response(200, json={"object": {"sha": f["head_sha"]}})
        if method == "GET" and path.startswith(f"/repos/{repo}/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": "t" * 40}})
        if method == "POST" and path == f"/repos/{repo}/git/trees":
            self.trees.append(body)
            return httpx.Response(201, json={"sha": "e" * 40})
        if method == "POST" and path == f"/repos/{repo}/git/commits":
            self.commits_created.append(body)
            return httpx.Response(201, json={"sha": "c" * 40})
        if method == "PATCH" and path.startswith(f"/repos/{repo}/git/refs/heads/"):
            if self.ref_conflict:
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            assert body["force"] is False
            f["head_sha"] = body["sha"]
            return httpx.Response(200, json={})
        if method == "POST" and path == f"/repos/{repo}/git/refs":
            self.refs[body["ref"]] = body["sha"]
            return httpx.Response(201, json={})
        if method == "POST" and path.startswith(f"/repos/{repo}/actions/workflows/") and path.endswith("/dispatches"):
            self.dispatches.append({"workflow": path.split("/")[-2], **body})
            return httpx.Response(204)
        if method == "PUT" and path == f"/repos/{repo}/pulls/{pr}/merge":
            if self.merge_status:
                return httpx.Response(self.merge_status, json={"message": "nope"})
            assert body["sha"] == f["head_sha"]
            self.merged = True
            self.merge_call = body
            return httpx.Response(200, json={"sha": "9" * 40, "merged": True})
        if method == "PUT" and path == f"/repos/{repo}/pulls/{pr}/update-branch":
            return httpx.Response(202, json={})
        if method == "GET" and path.startswith(f"/repos/{repo}/commits/") and path.endswith("/pulls"):
            sha = path.split("/")[-2]
            return httpx.Response(200, json=[self.raw_pull()] if sha == f["head_sha"] else [])
        if method == "POST" and path == f"/repos/{repo}/pulls":
            pull = {"number": 900 + len(self.pulls_created), **body}
            self.pulls_created.append(pull)
            return httpx.Response(201, json=pull)
        if method == "POST" and path == f"/repos/{repo}/issues":
            issue = {"number": 950 + len(self.issues), **body}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        if method == "GET" and path.startswith(f"/repos/{repo}/issues/comments/"):
            return httpx.Response(200, json=f["chatops_comment"])
        if method == "POST" and path.endswith("/reactions"):
            self.reactions.append(int(path.split("/")[-2]))
            return httpx.Response(201, json={})
        return None

    def client(self) -> GitHubClient:
        http = httpx.Client(base_url=API, transport=httpx.MockTransport(self.handler))
        return GitHubClient("ghp_TESTTOKEN_should_never_leak", http=http, sleep=lambda s: None)

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_MODEL", "MAX_AGENT_STEPS",
                 "PR_SENTINEL_BOT_LOGIN", "SENTINEL_AUTONOMY"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
