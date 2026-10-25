"""Validate model findings against the real diff, compare with the previous review, and render the PR comment."""

from __future__ import annotations

import re

from urllib.parse import quote

from .schemas import ChangedFile, CIReport, Finding, ModelReview, PreviousReview, ReviewResult, finding_id

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


def added_lines(patch: str | None):
    """Yield (new_line_number, text) for every added line in a unified diff."""
    current = None
    for raw in (patch or "").splitlines():
        m = HUNK_RE.match(raw)
        if m:
            current = int(m.group(1))
            continue
        if current is None or raw.startswith("-") or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            yield current, raw[1:]
        current += 1


def removed_lines(patch: str | None) -> list[str]:
    return [raw[1:] for raw in (patch or "").splitlines() if raw.startswith("-") and not raw.startswith("---")]


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


def ground_findings(
    review: ModelReview, files: list[ChangedFile], max_findings: int, context_paths: set[str] | frozenset = frozenset()
) -> tuple[list[Finding], list[str]]:
    """Drop findings on files the agent never saw, null out ungrounded lines, dedupe by id. Returns (findings, notes).

    `context_paths` are related files actually read at the head SHA. Findings there are kept, but always with
    line=None: only changed-diff lines count as verified locations."""
    by_path = {f.path: f for f in files}
    notes: list[str] = []
    merged: dict[str, Finding] = {}
    for mf in review.open_findings:
        changed = by_path.get(mf.path)
        if changed is None and mf.path not in context_paths:
            notes.append(f"dropped finding on unknown file {mf.path!r}")
            continue
        patch = changed.patch if changed else None
        line = mf.line if mf.line is not None and mf.line in diff_line_numbers(patch) else None
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


def render_review_comment(result: ReviewResult, state_block: str = "", owners: list[str] | None = None) -> str:
    from .state import MARKER  # local import avoids a cycle

    out = [MARKER, "## PR Sentinel Review", "", f"Reviewed commit: {_code(result.head_sha)}", ""]
    out.append(_escape(result.summary))
    out.append("")
    if result.triage is not None:
        out += render_triage_sections(result, owners or [])

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

    if result.autofix is not None and result.autofix.patch_preview and not result.autofix.applied:
        out += render_suggested_fix(result.autofix.patch_preview, result.autofix.reason)

    if result.ci is not None:
        out += render_ci_section(result.ci) + [""]

    if result.escalation is not None and result.status == "needs_human":
        e = result.escalation
        out.append("### Human review needed")
        out.append(f"Reason: {_escape(e.reason, single_line=True, limit=400)}")
        if e.tried:
            out.append(f"Checked: {_escape(', '.join(e.tried), single_line=True, limit=500)}")
        if e.unknown:
            out.append(f"Still unknown: {_escape(e.unknown, single_line=True, limit=400)}")
        if e.next_step:
            out.append(f"Next step: {_escape(e.next_step, single_line=True, limit=400)}")
        out.append("")

    related = [c for c in result.context_files if c.via == "related"]
    if related:
        out.append("<sub>Related files read for context: " + ", ".join(_code(c.path) for c in related[:10]) + "</sub>")
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


CHECK_ICON = {"passed": "✅", "failed": "❌", "pending": "⏳", "neutral": "⚪"}
CHECK_LABEL = {"passed": "Passed", "failed": "Failed", "pending": "Pending", "neutral": "Skipped/neutral"}


def render_ci_section(ci: CIReport) -> list[str]:
    out = [f"### CI checks (reviewed head {_code(ci.head_sha[:7])})"]
    if ci.state == "unavailable":
        out.append(f"CI status unavailable: {_escape(ci.error or 'unknown error', single_line=True, limit=300)}. "
                   "This is **not** a pass.")
        return out
    if not ci.checks:
        out.append("No CI checks were found for this commit at review time (PR Sentinel's own check is excluded).")
    for c in ci.checks[:15]:
        detail = ""
        if c.state == "failed":
            first = next((a for a in c.annotations if a.level == "failure"), None)
            if first:
                where = _code(first.path) + (f" line {first.line}" if first.line else "")
                detail = f" — {_escape(first.message, single_line=True, limit=200)} ({where})"
            elif c.summary:
                detail = f" — {_escape(c.summary, single_line=True, limit=200)}"
        # Only github.com URLs reach here (ci._safe_url); percent-encode so text can't break out of the link.
        link = f" ([details]({quote(c.url, safe=':/?#=&%.-_~')}))" if c.url else ""
        out.append(f"- {CHECK_ICON[c.state]} {_escape(c.name, single_line=True, limit=100)}: {CHECK_LABEL[c.state]}"
                   f"{detail}{link}")
    if len(ci.checks) > 15 or ci.truncated:
        out.append("- …more checks not shown (list truncated).")
    if ci.likely_cause:
        out.append(f"Likely cause (Claude's interpretation): {_escape(ci.likely_cause, single_line=True, limit=400)}")
    elif ci.state in ("failed", "mixed"):
        out.append("Likely cause: not determined from the available CI evidence.")
    if ci.recommended_next_step:
        out.append(f"Next step: {_escape(ci.recommended_next_step, single_line=True, limit=300)}")
    if ci.state in ("pending", "mixed"):
        out.append("<sub>Checks still running are shown as pending. This comment is not updated when they finish; "
                   "it refreshes on the next push.</sub>")
    return out


# ---------------- Milestone 3 sections ----------------

DECISION_TEXT = {
    "skip": "Skipped", "hold": "On hold: review only", "review_only": "Review only: a person merges",
    "auto_merge": "Merge automatically", "fix_then_merge": "Fix, then merge when CI passes",
    "suggest_fix": "Suggest a fix (not pushed); a person merges", "escalate_owner": "Owner review needed",
    "block": "Blocked", "waiting": "Waiting before merging",
}
TIER_TEXT = {"T0": "T0 trivial", "T1": "T1 low risk", "T2": "T2 moderate", "T3": "T3 high risk", "BLOCK": "BLOCK"}
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _value(v) -> str:
    return ", ".join(map(str, v[:3])) + ("…" if len(v) > 3 else "") if isinstance(v, list) else str(v)


def render_triage_sections(result: ReviewResult, owners: list[str]) -> list[str]:
    t = result.triage
    out = []
    if t.tier == "BLOCK":
        out += ["> [!CAUTION]", "> **Blocked.** " + _escape("; ".join(
            s.name + (f" at {s.value}" if isinstance(s.value, str) else "") for s in t.signals
            if s.name in ("secret", "hidden_unicode", "encoded_exec", "minified_code")), single_line=True, limit=500)
                + ". Secret values are never shown. If a credential was committed, rotate it.", ""]
    out.append("### Triage")
    claude = f", Claude {t.claude_tier}" if t.claude_tier else ""
    out.append(f"**{_escape(t.category, single_line=True)}** · {TIER_TEXT[t.tier]} (deterministic floor "
               f"{t.deterministic_floor}{claude}) · size {t.size} · confidence {t.confidence}")
    shown = [s for s in t.signals if s.name not in ("size", "ast_equivalence", "claude_triage")][:3]
    if shown:
        out.append("Signals: " + "; ".join(_escape(f"{s.name}: {_value(s.value)}", single_line=True, limit=160)
                                           for s in shown))
    out.append(f"Decision: **{DECISION_TEXT[t.decision]}** (autonomy `{t.autonomy}`)")
    if t.blocked_by:
        out.append("Not merging because: " + _escape(", ".join(t.blocked_by), single_line=True, limit=400))
    for note in t.notes:
        out.append(f"- {_escape(note, single_line=True, limit=300)}")
    out.append("")
    if result.actions:
        out.append("### Actions taken")
        for a in result.actions:
            extra = a.model_extra or {}
            detail = extra.get("sha") or extra.get("merge_sha") or ", ".join(extra.get("labels") or
                                                                              extra.get("reviewers") or [])
            out.append(f"- {_escape(a.type, single_line=True)}" + (f": {_escape(str(detail)[:80], single_line=True)}"
                                                                      if detail else ""))
        out.append("")
    if t.would_have:
        out.append("### Would have" if t.autonomy == "shadow" else "### Not done")
        out += [f"- {_escape(w, single_line=True, limit=200)}" for w in t.would_have]
        out.append("")
    if t.waiting_for:
        out.append("### Waiting for")
        out += [f"- {_escape(w, single_line=True, limit=200)}" for w in t.waiting_for]
        out.append("")
    if t.decision in ("escalate_owner", "block") and t.owner_review_guide is not None:
        g = t.owner_review_guide
        mentions = " ".join(f"@{o}" for o in owners if LOGIN_RE.match(o))
        out.append("### Owner review needed")
        if mentions:
            out.append(f"{mentions}: this PR needs your review before it can merge.")
        out.append(_escape(g.change_summary))
        for title, items in (("Key risks", g.key_risks), ("Review first", g.review_first),
                             ("Questions for the author", g.questions_for_author), ("Suggested tests", g.suggested_tests)):
            if items:
                out.append(f"**{title}:** " + "; ".join(_escape(i, single_line=True, limit=200) for i in items[:6]))
        out.append("")
    return out


def render_suggested_fix(preview: str, reason: str) -> list[str]:
    """Patch preview in a fence the content can't break out of; HTML comments can't fake our state block."""
    body = preview.replace("<!--", "<\u200b!--")
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", body)), default=0) + 1)
    return ["### Suggested fix (not pushed)", _escape(reason, single_line=True, limit=300),
            f"{fence}diff", body, fence, ""]
