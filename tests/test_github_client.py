import httpx
import pytest

from pr_agent.github_client import API, GitHubClient, GitHubError

TOKEN = "ghp_TESTTOKEN_should_never_leak"


def client(handler):
    http = httpx.Client(base_url=API, transport=httpx.MockTransport(handler))
    return GitHubClient(TOKEN, http=http, sleep=lambda s: None)


def test_changed_files_paginate():
    pages = {1: [{"filename": f"f{i}.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x"} for i in range(100)],
             2: [{"filename": "last.py", "status": "added"}]}

    def handler(req):
        return httpx.Response(200, json=pages[int(req.url.params["page"])])

    files = client(handler).get_changed_files("o/r", 1)
    assert len(files) == 101
    assert files[-1].path == "last.py" and files[-1].patch is None


def test_retries_transient_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(502)
        return httpx.Response(200, json={"title": "t", "base": {"sha": "b"}, "head": {"sha": "h"}})

    pr = client(handler).get_pull_request("o/r", 7)
    assert pr.head_sha == "h" and len(calls) == 3


def test_errors_never_include_token():
    def handler(req):
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubError) as exc:
        client(handler).get_pull_request("o/r", 1)
    assert exc.value.status == 401
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)


def test_read_file_pins_sha_and_quotes_path():
    seen = {}

    def handler(req):
        seen["path"], seen["ref"] = req.url.raw_path.decode(), req.url.params["ref"]
        return httpx.Response(200, content=b"print('hi')\n")

    text = client(handler).read_file("o/r", "src/my file.py", "deadbeef")
    assert text == "print('hi')\n"
    assert seen["ref"] == "deadbeef"
    assert seen["path"].startswith("/repos/o/r/contents/src/my%20file.py")


def test_read_file_refuses_binary():
    with pytest.raises(GitHubError):
        client(lambda req: httpx.Response(200, content=b"\x89PNG\x00\x00")).read_file("o/r", "a.png", "sha")


def test_invalid_repo_rejected():
    with pytest.raises(ValueError):
        client(lambda req: httpx.Response(200, json={"title": "", "base": {"sha": "b"}, "head": {"sha": "h"}})) \
            .get_pull_request("not a repo", 1)
