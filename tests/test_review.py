from pr_agent.review import diff_line_numbers, ground_findings, render_review_comment
from pr_agent.schemas import ChangedFile, Finding, ModelFinding, ModelReview, ReviewResult, finding_id
from pr_agent.state import MARKER
from pr_agent.tool_registry import select_files
from pr_agent.config import Limits

PATCH = "@@ -1,3 +1,4 @@\n a = 1\n-b = 2\n+b = 3\n+c = 4\n d = 5\n@@ -20,2 +21,2 @@\n x\n+y"


def test_diff_line_numbers_follow_new_side():
    assert diff_line_numbers(PATCH) == {1, 2, 3, 4, 21, 22}
    assert diff_line_numbers(None) == set()


def test_finding_id_is_stable_and_normalized():
    assert finding_id("a.py", "Null Deref!") == "a.py:null-deref"
    assert finding_id("a.py", "") == "a.py:issue"


# Spec test 3
def test_missing_patch_is_unreviewed_not_clean():
    files = [ChangedFile(path="a.py", patch=PATCH), ChangedFile(path="big.bin", patch=None),
             ChangedFile(path="gone.py", status="removed", patch=None)]
    reviewable, unreviewed = select_files(files, Limits())
    assert [f.path for f in reviewable] == ["a.py"]
    assert unreviewed == ["big.bin"]


def test_file_and_diff_caps_mark_files_unreviewed():
    files = [ChangedFile(path=f"f{i}.py", patch=PATCH) for i in range(4)]
    reviewable, unreviewed = select_files(files, Limits(max_files=2))
    assert len(reviewable) == 2 and unreviewed == ["f2.py", "f3.py"]
    reviewable, unreviewed = select_files(files, Limits(max_diff_chars=len(PATCH) + 1))
    assert len(reviewable) == 1 and len(unreviewed) == 3


def test_duplicate_findings_are_merged_keeping_highest_severity():
    base = dict(path="a.py", line=3, kind="null-deref", title="t", explanation="e", suggestion="s", confidence="low")
    review = ModelReview(summary="s", open_findings=[ModelFinding(severity="low", **base),
                                                     ModelFinding(severity="high", **base)])
    findings, _ = ground_findings(review, [ChangedFile(path="a.py", patch=PATCH)], max_findings=8)
    assert len(findings) == 1 and findings[0].severity == "high"


def test_render_neutralizes_marker_spoofing_and_mentions():
    evil = f"{MARKER} <script>x</script> @octocat"
    result = ReviewResult(
        repo="o/r", pr_number=1, head_sha="abc", status="completed", summary=evil,
        open_findings=[Finding(id="a.py:x", path="a`.py", line=None, severity="high", title=evil,
                               explanation=evil, suggestion=evil, confidence="high")],
    )
    body = render_review_comment(result)
    assert body.count(MARKER) == 1 and body.startswith(MARKER)
    assert "<script>" not in body
    assert "@octocat" not in body
