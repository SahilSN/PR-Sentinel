"""Milestone 2 phase D: the static dashboard renders only recorded data, safely."""

import json

import pytest

from pr_agent.cli import main as cli_main
from pr_agent.dashboard import load_trace, main, render_dashboard
from pr_agent.schemas import Finding, ReviewResult

from conftest import FIXTURES

EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'


def run_fixture(name, tmp_path):
    out = tmp_path / name
    cli_main(["--fixture", str(FIXTURES / f"{name}.json"), "--fake-model", "--dry-run", "--output-dir", str(out)])
    return out


def base(**kw):
    return ReviewResult(**{"repo": "o/r", "pr_number": 3, "head_sha": "a" * 40, "status": "completed",
                           "summary": "ok", **kw})


def test_dashboard_from_a_real_offline_run(clean_env, tmp_path):
    out = run_fixture("ci_failure_pr", tmp_path)
    assert main(["--input", str(out / "run_result.json"), "--trace", str(out / "actions.jsonl"),
                 "--output", str(out / "dashboard.html"), "--demo-data"]) == 0
    html = (out / "dashboard.html").read_text()
    assert "Demo data" in html and "Completed" in html
    assert "Possible None dereference" in html and "unit-tests" in html and "Likely cause" in html
    assert "get_ci_status" in html and "read_file" in html  # tool activity from the trace
    assert "of 10" in html  # tool-call limit recorded in the trace
    assert "<script" not in html


@pytest.mark.parametrize("status,label", [("completed", "Completed"), ("needs_human", "Needs a human"),
                                          ("budget_exceeded", "Budget exceeded"), ("error", "Error")])
def test_status_variants(status, label):
    html = render_dashboard(base(status=status, stop_reason=None if status == "completed" else "why"), [])
    assert f">{label}<" in html


def test_untrusted_strings_are_escaped():
    f = Finding(id="x:y", path=EVIL, severity="high", title=EVIL, explanation=EVIL, suggestion=EVIL, confidence="low")
    trace = [{"run_id": "r", "event": "run_start", "repo": "o/r", "pr_number": 3, "head_sha": "a" * 40},
             {"run_id": "r", "event": "tool_call", "step": 1, "tool": EVIL, "args": {"path": EVIL}, "ok": False,
              "error": EVIL}]
    html = render_dashboard(base(summary=EVIL, open_findings=[f]), trace)
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_v1_result_without_ci_renders(tmp_path):
    v1 = {"schema_version": 1, "repo": "o/r", "pr_number": 1, "head_sha": "h", "status": "completed", "summary": "s",
          "open_findings": [], "resolved_findings": [], "unreviewed_files": [], "stop_reason": None,
          "stats": {"llm_calls": 1, "tool_calls": 0, "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}}
    (tmp_path / "r.json").write_text(json.dumps(v1))
    assert main(["--input", str(tmp_path / "r.json"), "--trace", str(tmp_path / "missing.jsonl"),
                 "--output", str(tmp_path / "d.html")]) == 0
    html = (tmp_path / "d.html").read_text()
    assert "No CI data was recorded" in html and "No trace was recorded" in html


def test_no_invented_cost_or_history():
    html = render_dashboard(base(), [])
    assert "$" not in html and "No cost estimate" in html and "not a run history" in html


def test_long_strings_and_bad_links():
    from pr_agent.schemas import CheckRun, CIReport
    ci = CIReport(head_sha="a" * 40, state="failed", checks=[
        CheckRun(id=1, name="x" * 500, state="failed", status="completed", url="javascript:alert(1)", summary="y" * 5000)])
    html = render_dashboard(base(ci=ci), [])
    assert "javascript:" not in html and "x" * 500 in html


def test_trace_from_another_run_is_not_shown(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"run_id": "old", "event": "run_start", "repo": "o/r", "pr_number": 3, "head_sha": "b" * 40},
        {"run_id": "old", "event": "tool_call", "tool": "read_file", "ok": True},
        "not json",
    ]) + "\nnot json\n")
    assert load_trace(path, base()) == []


def test_live_result_links_to_pr_but_demo_does_not():
    assert 'href="https://github.com/o/r/pull/3"' in render_dashboard(base(), [])
    assert "href=" not in render_dashboard(base(), [], demo=True)


def test_missing_input_is_a_clear_error(tmp_path, capsys):
    assert main(["--input", str(tmp_path / "nope.json"), "--output", str(tmp_path / "d.html")]) == 2
    assert "could not read run result" in capsys.readouterr().err
