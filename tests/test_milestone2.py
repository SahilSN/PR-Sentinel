"""Milestone 2: CI evidence (A), related-file context (B), escalation (C). All offline: fixtures, fakes, mocks."""

import json

import httpx
import pytest

from pr_agent.agent import Tracer, run_review
from pr_agent.ci import build_ci_report, check_state, ci_report_from_fixture, fetch_ci_report, overall_state
from pr_agent.claude_client import FakeModel, ScriptedModel, text_response, tool_response
from pr_agent.cli import EXIT_OK, main
from pr_agent.config import Limits, Settings
from pr_agent.github_client import API, GitHubClient, GitHubError
from pr_agent.review import render_review_comment
from pr_agent.schemas import ChangedFile, CheckRun, PreviousReview, PullRequestInfo, ReviewResult
from pr_agent.state import MARKER, decode_state
from pr_agent.tool_registry import RepoEntry, python_import_candidates

from conftest import FIXTURES, FakeGitHub, load_fixture

SHA = "f" * 40
OLD_SHA = "e" * 40


def raw(id, name="unit-tests", status="completed", conclusion="success", sha=SHA, **output):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion, "head_sha": sha,
            "html_url": f"https://github.com/o/r/runs/{id}", "output": output}


def report(runs, annotations=None, total=None, settings=None):
    return build_ci_report(SHA, runs, len(runs) if total is None else total, annotations or {}, settings or Settings())


def setup(name):
    data = load_fixture(name)
    pr = PullRequestInfo(**{k: data[k] for k in ("repo", "pr_number", "title", "base_sha", "head_sha")})
    files = [ChangedFile(**f) for f in data["changed_files"]]
    previous = PreviousReview.model_validate(data["previous_review"]) if data.get("previous_review") else None
    contents = data.get("file_contents", {})
    reads = []

    def read_source(path):
        reads.append(path)
        return contents[path]

    tree = lambda: [RepoEntry(p, len(c)) for p, c in contents.items()]  # noqa: E731
    ci = ci_report_from_fixture(data["ci"], pr.head_sha, Settings()) if data.get("ci") else None
    return pr, files, previous, read_source, tree, ci, reads


def review(name, model=None, limits=None, ci="fixture", **kw):
    pr, files, previous, read_source, tree, fixture_ci, reads = setup(name)
    tracer = Tracer()
    result = run_review(pr, files, previous, model=model or FakeModel(), read_source=read_source,
                        limits=limits or Limits(), tracer=tracer, list_repo_files=tree,
                        ci=fixture_ci if ci == "fixture" else ci, **kw)
    return result, reads, tracer


def final(**overrides):
    body = {"summary": "ok", "open_findings": [], "resolved_finding_ids": [], "needs_human": False,
            "needs_human_reason": None, "ci_likely_cause": None, "ci_next_step": None, **overrides}
    return text_response(json.dumps(body))


# ---------------- Phase A: CI evidence ----------------


@pytest.mark.parametrize("status,conclusion,expected", [
    ("queued", None, "pending"), ("in_progress", None, "pending"), ("completed", "success", "passed"),
    ("completed", "failure", "failed"), ("completed", "timed_out", "failed"), ("completed", "skipped", "neutral"),
    ("completed", "cancelled", "neutral"), ("completed", None, "neutral"),
])
def test_check_state_classification(status, conclusion, expected):
    assert check_state(status, conclusion) == expected


def test_single_failed_check_with_annotation():
    ci = report([raw(1, conclusion="failure", title="1 failed")],
                {1: [{"path": "app/auth.py", "start_line": 7, "message": "AttributeError"}]})
    assert ci.state == "failed"
    assert ci.checks[0].annotations[0].path == "app/auth.py" and ci.checks[0].annotations[0].line == 7
    body = "\n".join(render_review_comment(ReviewResult(repo="o/r", pr_number=1, head_sha=SHA, status="completed",
                                                        summary="s", ci=ci)).splitlines())
    assert "❌ unit-tests: Failed — AttributeError" in body and "Likely cause: not determined" in body


def test_failed_passed_and_running_checks_are_distinct():
    ci = report([raw(1, "unit-tests", conclusion="failure"), raw(2, "lint"), raw(3, "build", "in_progress", None)])
    assert [c.state for c in ci.checks] == ["failed", "passed", "pending"]
    assert ci.state == "mixed"
    body = render_review_comment(ReviewResult(repo="o/r", pr_number=1, head_sha=SHA, status="completed",
                                              summary="s", ci=ci))
    assert "build: Pending" in body and "lint: Passed" in body and "not updated when they finish" in body


def test_no_checks_is_not_found_never_passed():
    ci = report([])
    assert ci.state == "not_found"
    assert overall_state([CheckRun(id=1, name="x", state="neutral", status="completed")]) == "not_found"
    body = render_review_comment(ReviewResult(repo="o/r", pr_number=1, head_sha=SHA, status="completed",
                                              summary="s", ci=ci))
    assert "No CI checks were found" in body and "Passed" not in body


def test_permission_denied_is_unavailable_not_clean():
    data = {**load_fixture("ci_failure_pr.json"), "ci": {"error": {"status": 403, "message": "Resource not accessible by integration"}}}
    gh = FakeGitHub(data).client()
    ci = fetch_ci_report(gh, data["repo"], data["head_sha"], Settings())
    assert ci.state == "unavailable" and "checks: read" in ci.error
    body = render_review_comment(ReviewResult(repo="o/r", pr_number=1, head_sha=SHA, status="completed",
                                              summary="s", ci=ci))
    assert "CI status unavailable" in body and "**not** a pass" in body


def test_own_failing_check_is_ignored_as_root_cause():
    ci = report([raw(1, "PR Sentinel review", conclusion="failure")])
    assert ci.checks == [] and ci.state == "not_found"
    assert ci.ignored_checks == ["PR Sentinel review (PR Sentinel's own check)"]


def test_checks_for_another_commit_are_not_used():
    ci = report([raw(1, conclusion="failure", sha=OLD_SHA), raw(2)])
    assert [c.name for c in ci.checks] == ["unit-tests"] and ci.state == "passed"
    assert "different commit eeeeeee" in ci.ignored_checks[0]


def test_pagination_and_truncation():
    runs = [raw(i, f"check-{i}") for i in range(1, 61)]
    pages = []

    def handler(req):
        per_page, page = int(req.url.params["per_page"]), int(req.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json={"total_count": 60, "check_runs": runs[(page - 1) * per_page: page * per_page]})

    gh = GitHubClient("t", http=httpx.Client(base_url=API, transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    got, total = gh.get_check_runs("o/r", SHA, limit=30)
    assert len(got) == 30 and total == 60 and pages == [1]
    ci = report(got, total=total)
    assert ci.truncated and len(ci.checks) == 30

    long = report([raw(1, conclusion="failure", title="x" * 5000)],
                  {1: [{"path": "a.py", "start_line": i, "message": "m" * 900} for i in range(1, 40)]})
    check = long.checks[0]
    assert len(check.summary) == Settings().limits.max_check_summary_chars and check.truncated
    assert len(check.annotations) == Settings().limits.max_annotations
    assert all(len(a.message) <= 500 for a in check.annotations)


def test_only_full_head_sha_is_ever_queried():
    gh = GitHubClient("t", http=httpx.Client(base_url=API, transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    for bad in ("main", "HEAD", "abc123", "../x"):
        with pytest.raises(GitHubError):
            gh.get_check_runs("o/r", bad, 30)
        with pytest.raises(GitHubError):
            gh.get_tree("o/r", bad)


def test_non_github_urls_are_dropped():
    ci = report([{**raw(1), "html_url": "https://evil.example/x)", "details_url": "javascript:alert(1)"}])
    assert ci.checks[0].url is None


def test_ci_failure_demo_links_check_to_finding():
    result, _, tracer = review("ci_failure_pr.json")
    assert result.status == "completed"
    assert result.ci.state == "failed" and result.ci.likely_cause and "app/auth.py" in result.ci.likely_cause
    assert result.ci.ignored_checks == ["PR Sentinel review (PR Sentinel's own check)"]
    assert any(e.get("tool") == "get_ci_status" and e["ok"] for e in tracer.events if e["event"] == "tool_call")


def test_pending_ci_gets_no_likely_cause():
    result, _, _ = review("ci_pending_pr.json")
    assert result.status == "completed" and result.ci.state == "pending"
    assert result.ci.likely_cause is None and result.ci.recommended_next_step is None


def test_model_cannot_attach_ci_interpretation_when_nothing_failed():
    result, _, _ = review("ci_pending_pr.json", model=ScriptedModel([final(ci_likely_cause="flaky", ci_next_step="x")]))
    assert result.ci.likely_cause is None


def test_ci_tool_counts_against_tool_budget():
    model = ScriptedModel([tool_response(("get_ci_status", {}), ("read_file", {"path": "app/auth.py"})), final()])
    result, _, _ = review("ci_failure_pr.json", model=model, limits=Limits(max_tool_calls=1))
    assert result.status == "budget_exceeded" and result.escalation.reason_code == "LIMIT_REACHED"


def test_v1_result_without_new_fields_still_validates():
    v1 = {"schema_version": 1, "repo": "o/r", "pr_number": 1, "head_sha": "h", "status": "completed",
          "summary": "s", "open_findings": [], "resolved_findings": [], "unreviewed_files": [], "stop_reason": None,
          "stats": {"llm_calls": 1, "tool_calls": 0, "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}}
    result = ReviewResult.model_validate(v1)
    assert result.ci is None and result.escalation is None and result.context_files == []
    assert "### CI checks" not in render_review_comment(result)


def test_live_publish_includes_ci_in_the_single_comment(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    fake = FakeGitHub(load_fixture("ci_failure_pr.json"))
    args = ["--repo", "demo/agent-playground", "--pr", "21", "--fake-model", "--publish", "--output-dir", str(tmp_path)]
    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert main(args + ["--force"], gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert len(fake.comments) == 1
    body = fake.comments[0]["body"]
    assert body.count(MARKER) == 1 and body.count("### CI checks") == 1 and "Likely cause" in body
    saved = json.loads((tmp_path / "run_result.json").read_text())
    assert saved["ci"]["state"] == "failed" and saved["ci"]["checks"][0]["name"] == "unit-tests"
    assert decode_state(body).head_sha == fake.fixture["head_sha"]


# ---------------- Phase B: related-file context ----------------


def test_cross_file_bug_found_via_related_helper():
    result, reads, tracer = review("cross_file_pr.json")
    assert result.status == "completed"
    assert [f.id for f in result.open_findings] == ["app/profile_view.py:null-deref"]
    assert "app/profiles.py" in result.open_findings[0].explanation
    assert [(c.path, c.via) for c in result.context_files] == [("app/profile_view.py", "changed"),
                                                               ("app/profiles.py", "related")]
    assert all(c.sha == result.head_sha for c in result.context_files)
    assert ".env" not in json.dumps(tracer.events)  # the injected comment in app/profiles.py was not obeyed


def test_cross_file_bug_is_missed_without_the_helper():
    """Control: the same change without related-file access has no evidence for the bug."""
    pr, files, previous, read_source, _, _, _ = setup("cross_file_pr.json")
    result = run_review(pr, files, previous, model=FakeModel(), read_source=read_source, limits=Limits())
    assert result.open_findings == []


def test_import_resolution():
    src = "import app.db\nfrom app import models\nfrom .helpers import fmt\nfrom ..core.base import Base\n"
    cands = python_import_candidates("pkg/sub/mod.py", src)
    for expected in ("app/db.py", "app/__init__.py", "app/models.py", "pkg/sub/helpers.py", "pkg/sub/helpers/fmt.py",
                     "pkg/core/base.py", "src/app/db.py"):
        assert expected in cands
    assert python_import_candidates("x.py", "def (:") == []


def test_related_files_must_be_listed_before_reading():
    model = ScriptedModel([
        tool_response(("read_file", {"path": "app/profiles.py"})),
        tool_response(("list_related_files", {"path": "app/profile_view.py"})),
        tool_response(("read_file", {"path": "app/profiles.py"})),
        final(),
    ])
    result, reads, tracer = review("cross_file_pr.json", model=model)
    errors = [e for e in tracer.events if e["event"] == "tool_call" and not e["ok"]]
    assert "not a reviewable file" in errors[0]["error"]
    listed = json.loads(model.requests[2][-1]["content"][0]["content"])
    assert {r["path"]: r["reason"] for r in listed["related_files"]} == {
        "app/profiles.py": "imported by the changed file",
        "tests/test_profile_view.py": "tests for the changed module",
        "app/__init__.py": "same directory",
    }
    assert reads.count("app/profiles.py") == 1 and result.status == "completed"


def test_related_listing_rejects_unchanged_and_traversal_paths():
    model = ScriptedModel([
        tool_response(("list_related_files", {"path": "app/profiles.py"}),
                      ("list_related_files", {"path": "../../etc/passwd"})),
        final(),
    ])
    _, _, tracer = review("cross_file_pr.json", model=model)
    errors = [e["error"] for e in tracer.events if e["event"] == "tool_call"]
    assert "not a changed file" in errors[0] and "leave the repository" in errors[1]


def test_related_reads_are_capped():
    model = ScriptedModel([
        tool_response(("list_related_files", {"path": "app/profile_view.py"})),
        tool_response(("read_file", {"path": "app/profiles.py"}), ("read_file", {"path": "app/__init__.py"})),
        final(),
    ])
    _, reads, tracer = review("cross_file_pr.json", model=model, limits=Limits(max_context_reads=1))
    errors = [e["error"] for e in tracer.events if e["event"] == "tool_call" and not e["ok"]]
    assert errors == ["related-file read limit (1) reached"]


def test_symlinks_and_huge_files_are_never_related(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    data = load_fixture("cross_file_pr.json")
    data["extra_tree_entries"] = [
        {"path": "app/secrets_link.py", "type": "blob", "mode": "120000", "size": 10},
        {"path": "app/huge.py", "type": "blob", "mode": "100644", "size": 5_000_000},
    ]
    fake = FakeGitHub(data)
    model = ScriptedModel([tool_response(("list_related_files", {"path": "app/profile_view.py"})), final()])
    main(["--repo", "demo/agent-playground", "--pr", "23", "--output-dir", str(tmp_path), "--fake-model"],
         gh=fake.client(), model=model)
    listed = json.loads(model.requests[1][-1]["content"][0]["content"])
    paths = {r["path"] for r in listed["related_files"]}
    assert "app/profiles.py" in paths and "app/secrets_link.py" not in paths and "app/huge.py" not in paths


def test_findings_in_read_related_files_are_kept_without_line():
    finding = {"path": "app/profiles.py", "line": 7, "kind": "missing-default", "severity": "medium", "title": "t",
               "explanation": "e", "suggestion": "s", "confidence": "medium"}
    model = ScriptedModel([
        tool_response(("list_related_files", {"path": "app/profile_view.py"})),
        tool_response(("read_file", {"path": "app/profiles.py"})),
        final(open_findings=[finding]),
    ])
    result, _, _ = review("cross_file_pr.json", model=model)
    assert result.status == "completed"
    assert [(f.path, f.line) for f in result.open_findings] == [("app/profiles.py", None)]


# ---------------- Phase C: escalation ----------------


def test_open_findings_alone_do_not_escalate():
    result, _, _ = review("buggy_pr.json")
    assert result.status == "completed" and result.open_findings and result.escalation is None


def test_unverifiable_serious_finding_needs_human():
    ghost = {"path": "app/billing.py", "line": 3, "kind": "overcharge", "severity": "high", "title": "Double charge",
             "explanation": "e", "suggestion": "s", "confidence": "high"}
    result, _, _ = review("buggy_pr.json", model=ScriptedModel([final(open_findings=[ghost])]))
    assert result.status == "needs_human" and result.escalation.reason_code == "INSUFFICIENT_EVIDENCE"
    assert "app/billing.py" in result.escalation.next_step and result.escalation.tried
    assert result.open_findings == []  # the unverified claim is not published as a finding
    assert "### Human review needed" in render_review_comment(result)


def test_unexplained_ci_failure_is_conflicting_evidence():
    result, _, _ = review("ci_conflict_pr.json")
    assert result.status == "needs_human" and result.escalation.reason_code == "CONFLICTING_EVIDENCE"
    assert "app/text.py" in result.stop_reason
    assert "CI checks (failed)" in result.escalation.tried and result.escalation.next_step


def test_explained_ci_failure_does_not_escalate():
    result, _, _ = review("ci_conflict_pr.json", model=ScriptedModel([
        final(ci_likely_cause="slugify drops non-ASCII letters instead of transliterating them", ci_next_step="x")]))
    assert result.status == "completed" and result.ci.likely_cause.startswith("slugify")


def test_repeated_requests_escalate_with_code():
    same = ("read_file", {"path": "app/auth.py"})
    result, _, _ = review("buggy_pr.json", model=ScriptedModel([tool_response(same)] * 3 + [final()]))
    assert result.status == "needs_human" and result.escalation.reason_code == "REPEATED_TOOL_REQUEST"
    assert "read app/auth.py" in result.escalation.tried


def test_fetch_failures_escalate_as_tool_failures():
    pr, files, previous, _, tree, _, _ = setup("cross_file_pr.json")

    def broken(path):
        raise GitHubError(502, "Bad Gateway")

    model = ScriptedModel([tool_response(("read_file", {"path": "app/profile_view.py"}),
                                         ("list_related_files", {"path": "app/profile_view.py"})), final()])
    result = run_review(pr, files, previous, model=model, read_source=broken, limits=Limits(), list_repo_files=tree)
    assert result.status == "needs_human" and result.escalation.reason_code == "TOOL_FAILURES"
    assert "permissions" in result.escalation.next_step


def test_budget_exceeded_stays_budget_exceeded():
    result, _, _ = review("ci_failure_pr.json", limits=Limits(max_agent_steps=1))
    assert result.status == "budget_exceeded" and result.escalation.reason_code == "LIMIT_REACHED"
    assert "### Human review needed" not in render_review_comment(result)
    assert result.ci is not None and result.ci.state == "failed"  # evidence collected so far is kept


def test_failing_check_blocks_marking_a_finding_resolved():
    data = load_fixture("fixed_pr.json")
    pr = PullRequestInfo(**{k: data[k] for k in ("repo", "pr_number", "title", "base_sha", "head_sha")})
    ci = report([raw(1, conclusion="failure")], {1: [{"path": "app/auth.py", "start_line": 9, "message": "boom"}]})
    result = run_review(pr, [ChangedFile(**f) for f in data["changed_files"]],
                        PreviousReview.model_validate(data["previous_review"]), model=FakeModel(),
                        read_source=lambda p: data["file_contents"][p], limits=Limits(), ci=ci)
    assert result.resolved_findings == []
    assert result.open_findings[0].id == "app/auth.py:null-deref"


def test_escalation_updates_the_same_comment(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    fake = FakeGitHub(load_fixture("ci_conflict_pr.json"))
    args = ["--repo", "demo/agent-playground", "--pr", "24", "--fake-model", "--publish", "--output-dir", str(tmp_path)]
    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert "### Human review needed" in fake.comments[0]["body"]
    # needs_human isn't "completed", so the same commit is reviewed again, still in the one comment.
    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert len(fake.comments) == 1
    saved = json.loads((tmp_path / "run_result.json").read_text())
    assert saved["status"] == "needs_human" and saved["escalation"]["reason_code"] == "CONFLICTING_EVIDENCE"
    assert saved["stop_reason"]
