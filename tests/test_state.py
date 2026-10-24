import base64

from pr_agent.schemas import Finding, ReviewResult
from pr_agent.state import MARKER, decode_state, encode_state, find_bot_comment, upsert_review_comment

from conftest import FakeGitHub, load_fixture


def result():
    return ReviewResult(
        repo="o/r", pr_number=1, head_sha="abc123", status="completed", summary="s",
        open_findings=[Finding(id="a.py:x", path="a.py", severity="high", title="t", explanation="e --> <!--",
                               suggestion="s", confidence="high")],
    )


def test_state_roundtrip_and_no_comment_breakout():
    block = encode_state(result())
    assert block.count("-->") == 1  # the payload itself can't close the HTML comment
    state = decode_state(f"{MARKER}\nbody\n{block}")
    assert state.head_sha == "abc123" and state.open_findings[0].id == "a.py:x"


def test_malformed_state_is_ignored():
    bad = base64.b64encode(b'{"head_sha": 5}').decode()
    assert decode_state(f"<!-- pr-sentinel-state:v1 {bad} -->") is None
    assert decode_state("<!-- pr-sentinel-state:v1 !!!notbase64 -->") is None
    assert decode_state("no state here") is None


def test_marker_from_other_users_is_not_trusted():
    comments = [
        {"id": 1, "body": MARKER + " fake", "user": {"login": "mallory"}},
        {"id": 2, "body": MARKER + " real", "user": {"login": "github-actions[bot]"}},
    ]
    assert find_bot_comment(comments, "github-actions[bot]")["id"] == 2
    assert find_bot_comment(comments[:1], "github-actions[bot]") is None


# Spec test 8 (low level): upsert updates instead of creating a second comment
def test_upsert_creates_once_then_updates():
    fake = FakeGitHub(load_fixture("buggy_pr.json"))
    gh = fake.client()
    first = upsert_review_comment(gh, "demo/agent-playground", 12, MARKER + " v1", "github-actions[bot]")
    second = upsert_review_comment(gh, "demo/agent-playground", 12, MARKER + " v2", "github-actions[bot]")
    assert first == second
    assert len(fake.comments) == 1 and fake.comments[0]["body"].endswith("v2")
