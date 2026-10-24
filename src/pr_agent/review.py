"""Validate model findings against the real diff, compare with the previous review, and render the PR comment."""

from __future__ import annotations

import re

from .schemas import ChangedFile, Finding, ModelReview, PreviousReview, ReviewResult, finding_id

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


# ---------- Grounding ----------


def diff_line_numbers(patch: str | None) -> set[int]:
    """New-side line numbers that appear in a unified diff patch (added and context lines)."""
    lines: set[int] = set()
    if not patch:
        return lines
    current = None
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            current = int(m.group(1))
            continue
        if current is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        lines.add(current)
        current += 1
    return lines


def new_side_text(patch: str | None) -> str:
    """Best-effort reconstruction of visible new-side code from a patch (fixture mode has no full files)."""
    if not patch:
        return ""
    out = []
    for raw in patch.splitlines():
        if raw.startswith("@@") or raw.startswith("-") or raw.startswith("\\"):
            continue
        out.append(raw[1:] if raw[:1] in {"+", " "} else raw)
    return "\n".join(out)


def ground_findings(review: ModelReview, files: list[ChangedFile], max_findings: int) -> tuple[list[Finding], list[str]]:
    """Drop findings on files not in the PR, null out ungrounded lines, dedupe by id. Returns (findings, notes)."""
    by_path = {f.path: f for f in files}
    notes: list[str] = []
    merged: dict[str, Finding] = {}
    for mf in review.open_findings:
        changed = by_path.get(mf.path)
        if changed is None:
            notes.append(f"dropped finding on unknown file {mf.path!r}")
            continue
        line = mf.line if mf.line is not None and mf.line in diff_line_numbers(changed.patch) else None
        if mf.line is not None and line is None:
            notes.append(f"removed ungrounded line {mf.line} for {mf.path}")
        finding = Finding(
            id=finding_id(mf.path, mf.kind),
            path=mf.path,
            line=line,
            severity=mf.severity,
            title=mf.title,
            explanation=mf.explanation,
            suggestion=mf.suggestion,
            confidence=mf.confidence,
        )
        existing = merged.get(finding.id)
        if existing is None or SEVERITY_RANK[finding.severity] > SEVERITY_RANK[existing.severity]:
            merged[finding.id] = finding
    findings = sorted(merged.values(), key=lambda f: -SEVERITY_RANK[f.severity])
    if len(findings) > max_findings:
        notes.append(f"kept top {max_findings} of {len(findings)} findings")
    return findings[:max_findings], notes


def compare_with_previous(
    current: list[Finding], resolved_ids: list[str], previous: PreviousReview | None, head_sha: str | None = None
) -> tuple[list[Finding], list[Finding]]:
    """Split into (open, resolved). A previous finding is resolved only if the model says so with evidence
    and doesn't re-report it. If the model is silent about it, we keep it open at low confidence."""
    if previous is None:
        return current, []
    # times_seen counts distinct head SHAs: re-reviewing the same commit (after an error, or --force) adds nothing.
    seen_bump = 0 if head_sha is not None and previous.head_sha == head_sha else 1
    prev_by_id = {f.id: f for f in previous.open_findings}
    open_findings: list[Finding] = []
    for f in current:
        if f.id in prev_by_id:
            f = f.model_copy(update={"times_seen": prev_by_id[f.id].times_seen + seen_bump})
        open_findings.append(f)
    current_ids = {f.id for f in current}
    resolved: list[Finding] = []
    for fid, prev in prev_by_id.items():
        if fid in current_ids:
            continue
        if fid in resolved_ids:
            resolved.append(prev)
        else:
            open_findings.append(
                prev.model_copy(
                    update={
                        "confidence": "low",
                        "times_seen": prev.times_seen + seen_bump,
                        "explanation": prev.explanation.split(" (Carried over")[0]
                        + " (Carried over from an earlier commit; the fix could not be confirmed.)",
                    }
                )
            )
    return open_findings, resolved


def carry_forward(previous: PreviousReview | None) -> list[Finding]:
    """On a stopped/failed run, keep earlier findings visible rather than silently dropping them."""
    return list(previous.open_findings) if previous else []


# ---------- Rendering ----------


def _escape(text: str, single_line: bool = False, limit: int = 1500) -> str:
    """Neutralize HTML/comments (so text can't fake our marker), @mentions, and runaway length."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"@(?=\w)", "@​", text)
    if single_line:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _code(text: str) -> str:
    return "`" + _escape(text.replace("`", "'"), single_line=True, limit=200) + "`"


STATUS_LABEL = {
    "completed": "Completed",
    "needs_human": "Needs a human reviewer",
    "budget_exceeded": "Stopped: budget exceeded",
    "error": "Review failed",
}
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}


def render_review_comment(result: ReviewResult, state_block: str = "") -> str:
    from .state import MARKER  # local import avoids a cycle

    out = [MARKER, "## PR Sentinel Review", "", f"Reviewed commit: {_code(result.head_sha)}", ""]
    out.append(_escape(result.summary))
    out.append("")

    out.append(f"### Open findings ({len(result.open_findings)})")
    if not result.open_findings:
        out.append("No open findings in the reviewed changes.")
    for f in result.open_findings:
        where = _code(f.path) + (f" line {f.line}" if f.line is not None else "")
        repeat = f" · reported on {f.times_seen} commits" if f.times_seen > 1 else ""
        out.append(
            f"- {SEVERITY_ICON[f.severity]} **{_escape(f.title, single_line=True, limit=200)}** — {where} "
            f"({f.severity} severity, {f.confidence} confidence{repeat})"
        )
        out.append(f"  - {_escape(f.explanation, single_line=True)}")
        out.append(f"  - Suggestion: {_escape(f.suggestion, single_line=True)}")
    out.append("")

    if result.resolved_findings:
        out.append(f"### Resolved since last review ({len(result.resolved_findings)})")
        for f in result.resolved_findings:
            out.append(f"- ✅ ~~{_escape(f.title, single_line=True, limit=200)}~~ — {_code(f.path)}")
        out.append("")

    if result.unreviewed_files:
        out.append("### Not reviewed")
        out.append("These files were skipped (no diff available, binary, or over size limits). They are **not** approved.")
        for path in result.unreviewed_files[:30]:
            out.append(f"- {_code(path)}")
        out.append("")

    s = result.stats
    out.append("### Agent status")
    status_line = f"{STATUS_LABEL[result.status]}."
    if result.stop_reason:
        status_line += f" Reason: {_escape(result.stop_reason, single_line=True, limit=300)}"
    out.append(status_line)
    out.append(
        f"<sub>{s.llm_calls} model calls · {s.tool_calls} tool calls · "
        f"{s.input_tokens:,} input / {s.output_tokens:,} output tokens · {s.duration_ms / 1000:.1f}s. "
        "Automated review, not a security guarantee.</sub>"
    )
    if state_block:
        out += ["", state_block]
    return "\n".join(out)
