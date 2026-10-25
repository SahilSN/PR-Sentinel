"""Static, self-contained HTML dashboard for one recorded run (Milestone 2, phase D).

    python -m pr_agent.dashboard --input artifacts/run_result.json --trace artifacts/actions.jsonl \\
        --output artifacts/dashboard.html [--demo-data]

Shows only what the artifacts record: no history, no invented costs. Every string from the run (paths, findings,
check output, trace) is HTML-escaped, and there's no JavaScript.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from pydantic import ValidationError

from .schemas import Finding, ReviewResult

STATUS = {
    "completed": ("Completed", "ok"),
    "needs_human": ("Needs a human", "warn"),
    "budget_exceeded": ("Budget exceeded", "warn"),
    "error": ("Error", "bad"),
}
CHECK = {"passed": ("Passed", "ok"), "failed": ("Failed", "bad"), "pending": ("Pending", "muted"),
         "neutral": ("Skipped", "muted")}
CI_STATE = {"passed": "ok", "failed": "bad", "mixed": "bad", "pending": "muted", "not_found": "muted",
            "unavailable": "warn"}

CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--ok:#1f7a4d;--ok-bg:#e3f3ea;
--warn:#8a5a00;--warn-bg:#fbf0d9;--bad:#b3261e;--bad-bg:#fbe5e3;--muted-bg:#eeede8;--code:#f0efea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--text:#ecebe6;--muted:#a3a29b;
--line:#33332f;--ok:#6fd3a0;--ok-bg:#163527;--warn:#f0c46b;--warn-bg:#3a2e12;--bad:#ff8f86;--bad-bg:#40201d;
--muted-bg:#2a2a27;--code:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted)}
.meta{color:var(--muted);margin:4px 0 0;overflow-wrap:anywhere}code{background:var(--code);padding:1px 5px;
border-radius:4px;font-size:13px;overflow-wrap:anywhere}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
.hero{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:13px;white-space:nowrap}
.big{font-size:20px;padding:8px 18px}
.ok{background:var(--ok-bg);color:var(--ok)}.warn{background:var(--warn-bg);color:var(--warn)}
.bad{background:var(--bad-bg);color:var(--bad)}.muted{background:var(--muted-bg);color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{border:1px solid var(--line);border-radius:8px;padding:10px}.stat b{display:block;font-size:20px}
.stat span{color:var(--muted);font-size:13px}
details{border-top:1px solid var(--line);padding:10px 0}details:first-of-type{border-top:0}
summary{cursor:pointer;font-weight:600}ul{margin:6px 0;padding-left:20px}li{margin:4px 0}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:break-word}th{color:var(--muted);font-weight:600}
td:first-child,td:nth-child(2){white-space:nowrap}
.table{overflow-x:auto}.demo{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:10px 14px;
font-weight:600;margin-bottom:16px}a{color:inherit}.note{color:var(--muted);font-size:13px}
"""


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _link(url: str | None, text: str) -> str:
    if url and re.match(r"^https://github\.com/[\w.\-/#?=&%]+$", url):
        return f'<a href="{_e(url)}" rel="noopener noreferrer">{_e(text)}</a>'
    return _e(text)


def load_trace(path: Path | None, result: ReviewResult) -> list[dict]:
    """Events of the run that produced `result` (the last matching run_start), or [] if there's no trace."""
    if path is None or not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):  # skip valid-JSON lines that aren't events
            events.append(event)
    starts = [e for e in events if e.get("event") == "run_start" and e.get("repo") == result.repo
              and e.get("pr_number") == result.pr_number and e.get("head_sha") == result.head_sha]
    if not starts:
        return []
    run_id = starts[-1].get("run_id")
    return [e for e in events if e.get("run_id") == run_id]


def _finding(f: Finding, resolved: bool = False) -> str:
    where = f"<code>{_e(f.path)}</code>" + (f" line {f.line}" if f.line is not None else "")
    tag = '<span class="badge ok">resolved</span>' if resolved else f'<span class="badge {_sev(f.severity)}">{_e(f.severity)}</span>'
    seen = f" · reported on {f.times_seen} commits" if f.times_seen > 1 else ""
    return (f"<details><summary>{tag} {_e(f.title)}</summary><p>{where} · {_e(f.confidence)} confidence{seen}</p>"
            f"<p>{_e(f.explanation)}</p><p><b>Suggestion:</b> {_e(f.suggestion)}</p></details>")


def _sev(severity: str) -> str:
    return {"high": "bad", "medium": "warn"}.get(severity, "muted")


def render_dashboard(result: ReviewResult, trace: list[dict], demo: bool = False) -> str:
    label, tone = STATUS[result.status]
    s = result.stats
    start = next((e for e in trace if e.get("event") == "run_start"), {})
    end = next((e for e in reversed(trace) if e.get("event") == "run_end"), {})
    limits = start.get("limits") or {}
    finished = (datetime.fromtimestamp(end["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                if isinstance(end.get("ts"), (int, float)) else None)
    repo_ok = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result.repo) and not demo
    pr_url = f"https://github.com/{result.repo}/pull/{result.pr_number}" if repo_ok else None

    out = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>PR Sentinel run</title><style>{CSS}</style></head><body><main>"]
    if demo:
        out.append('<div class="demo">Demo data: generated from an offline fixture run, not a live PR.</div>')
    out.append('<section class="hero"><div>')
    out.append(f"<h1>{_link(pr_url, f'{result.repo} #{result.pr_number}')}</h1>")
    out.append(f'<p class="meta">Reviewed head <code>{_e(result.head_sha)}</code>'
               + (f" · finished {_e(finished)}" if finished else "") + "</p></div>")
    out.append(f'<span class="badge big {tone}">{_e(label)}</span></section>')

    out.append(f'<section class="panel"><h2>Summary</h2><p>{_e(result.summary)}</p>')
    if result.stop_reason:
        out.append(f"<p><b>Stop reason:</b> {_e(result.stop_reason)}</p>")
    if result.escalation:
        e = result.escalation
        out.append(f'<p><span class="badge warn">{_e(e.reason_code)}</span></p><ul>'
                   + (f"<li><b>Checked:</b> {_e(', '.join(e.tried))}</li>" if e.tried else "")
                   + (f"<li><b>Still unknown:</b> {_e(e.unknown)}</li>" if e.unknown else "")
                   + (f"<li><b>Recommended next step:</b> {_e(e.next_step)}</li>" if e.next_step else "")
                   + "</ul>")
    out.append("</section>")

    out += _triage_panel(result)

    out.append('<section class="panel"><h2>Findings</h2><div class="grid">'
               f'<div class="stat"><b>{len(result.open_findings)}</b><span>open</span></div>'
               f'<div class="stat"><b>{len(result.resolved_findings)}</b><span>resolved</span></div>'
               f'<div class="stat"><b>{len(result.unreviewed_files)}</b><span>files not reviewed</span></div></div>')
    out += [_finding(f) for f in result.open_findings] + [_finding(f, True) for f in result.resolved_findings]
    if result.unreviewed_files:
        out.append("<p><b>Not reviewed (not approved):</b> "
                   + ", ".join(f"<code>{_e(p)}</code>" for p in result.unreviewed_files) + "</p>")
    out.append("</section>")

    out.append('<section class="panel"><h2>CI checks</h2>')
    ci = result.ci
    if ci is None:
        out.append('<p class="note">No CI data was recorded for this run.</p>')
    else:
        out.append(f'<p>Overall: <span class="badge {CI_STATE[ci.state]}">{_e(ci.state)}</span>'
                   f" for <code>{_e(ci.head_sha[:12])}</code></p>")
        if ci.error:
            out.append(f"<p>{_e(ci.error)} This is not a pass.</p>")
        if ci.checks:
            out.append('<div class="table"><table><tr><th>Check</th><th>Result</th><th>Evidence</th></tr>')
            for c in ci.checks:
                text, cls = CHECK[c.state]
                notes = "".join(f"<li><code>{_e(a.path)}</code>{f' line {a.line}' if a.line else ''}: {_e(a.message)}</li>"
                                for a in c.annotations)
                evidence = (_e(c.summary) if c.summary else "") + (f"<ul>{notes}</ul>" if notes else "")
                out.append(f'<tr><td>{_link(c.url, c.name)}</td><td><span class="badge {cls}">{text}</span></td>'
                           f"<td>{evidence}</td></tr>")
            out.append("</table></div>")
        if ci.ignored_checks:
            out.append(f'<p class="note">Ignored: {_e(", ".join(ci.ignored_checks))}</p>')
        if ci.likely_cause:
            out.append(f"<p><b>Likely cause (Claude's interpretation):</b> {_e(ci.likely_cause)}</p>")
        if ci.recommended_next_step:
            out.append(f"<p><b>Next step:</b> {_e(ci.recommended_next_step)}</p>")
    out.append("</section>")

    out.append('<section class="panel"><h2>Agent activity</h2><div class="grid">')
    for value, name, key in [(s.llm_calls, "model calls", "max_llm_calls"), (s.tool_calls, "tool calls", "max_tool_calls"),
                             (s.input_tokens + s.output_tokens, "tokens", "max_total_tokens"),
                             (f"{s.duration_ms / 1000:.1f}s", "runtime", "max_runtime_seconds")]:
        limit = limits.get(key)
        cap = f" of {limit:,}{'s' if key == 'max_runtime_seconds' else ''}" if isinstance(limit, int) else ""
        shown = f"{value:,}" if isinstance(value, int) else value
        out.append(f'<div class="stat"><b>{_e(shown)}</b><span>{name}{_e(cap)}</span></div>')
    out.append("</div>")
    out.append(f'<p class="note">{s.input_tokens:,} input / {s.output_tokens:,} output tokens, as reported by the '
               "API. No cost estimate is shown.</p>")
    if result.context_files:
        out.append("<p><b>Files inspected:</b></p><ul>" + "".join(
            f"<li><code>{_e(c.path)}</code> ({_e(c.via)}, {c.chars:,} chars{', truncated' if c.truncated else ''})</li>"
            for c in result.context_files) + "</ul>")
    calls = [e for e in trace if e.get("event") == "tool_call"]
    if calls:
        out.append('<div class="table"><table><tr><th>Step</th><th>Tool</th><th>Input</th><th>Result</th></tr>')
        for e in calls:
            ok = '<span class="badge ok">ok</span>' if e.get("ok") else f'<span class="badge bad">refused</span> {_e(e.get("error"))}'
            out.append(f"<tr><td>{_e(e.get('step'))}</td><td><code>{_e(e.get('tool'))}</code></td>"
                       f"<td><code>{_e(json.dumps(e.get('args') or {}))}</code></td><td>{ok}</td></tr>")
        out.append("</table></div>")
    elif not trace:
        out.append('<p class="note">No trace was recorded for this run (actions.jsonl missing or from another run).</p>')
    out.append("</section>")

    out.append('<p class="note">Snapshot of one run from its artifacts; not a run history. '
               "Automated review, not a security guarantee.</p></main></body></html>")
    return "\n".join(out)


TIER_TONE = {"T0": "ok", "T1": "ok", "T2": "warn", "T3": "bad", "BLOCK": "bad"}


def _triage_panel(result: ReviewResult) -> list[str]:
    """Milestone 3: tier, category, signals, decision, actions taken or that would have been taken, owner guide."""
    t = result.triage
    if t is None:
        return []
    out = []
    if t.would_have and t.autonomy == "shadow":
        out.append('<div class="demo">Shadow mode: PR Sentinel only reports what it would have done.</div>')
    out.append('<section class="panel"><h2>Triage</h2>')
    out.append(f'<p><span class="badge big {TIER_TONE[t.tier]}">{_e(t.tier)}</span> <b>{_e(t.category)}</b> · size '
               f"{_e(t.size)} · floor {_e(t.deterministic_floor)} · Claude {_e(t.claude_tier or '-')} · "
               f"confidence {_e(t.confidence)}</p>")
    out.append(f"<p><b>Decision:</b> {_e(t.decision)} (autonomy <code>{_e(t.autonomy)}</code>)</p>")
    if t.blocked_by:
        out.append(f"<p><b>Not merging because:</b> {_e(', '.join(t.blocked_by))}</p>")
    if t.waiting_for:
        out.append("<p><b>Waiting for:</b></p><ul>" + "".join(f"<li>{_e(w)}</li>" for w in t.waiting_for) + "</ul>")
    if t.notes:
        out.append("<ul>" + "".join(f"<li>{_e(n)}</li>" for n in t.notes) + "</ul>")
    rows = "".join(
        f"<tr><td><code>{_e(s.name)}</code></td><td>{_e(', '.join(map(str, s.value)) if isinstance(s.value, list) else s.value)}"
        f"</td><td>{_e(s.source)}</td></tr>" for s in t.signals)
    out.append(f'<details><summary>Signals ({len(t.signals)})</summary><div class="table"><table>'
               f"<tr><th>Signal</th><th>Value</th><th>Source</th></tr>{rows}</table></div></details>")
    if result.actions or t.would_have:
        out.append("<p><b>Actions taken:</b></p><ul>" + "".join(
            f"<li><code>{_e(a.type)}</code> {_e(json.dumps(a.model_extra or {})[:200])}</li>" for a in result.actions)
                   + "".join(f'<li><span class="badge muted">would have</span> {_e(w)}</li>'
                             for w in (t.would_have or [])) + "</ul>")
    if result.autofix is not None:
        a = result.autofix
        status = "applied" if a.applied else ("rejected: " + (a.rejected_reason or "") if a.rejected_reason else "proposed")
        out.append(f"<p><b>Autofix attempt {a.attempt}:</b> {_e(status)}"
                   + (f" · commit <code>{_e(a.commit_sha[:12])}</code>" if a.commit_sha else "") + "</p>")
    g = t.owner_review_guide
    if g is not None and t.tier in ("T3", "BLOCK"):
        out.append(f"<p><b>Owner review guide:</b> {_e(g.change_summary)}</p><ul>"
                   + "".join(f"<li><b>{label}:</b> {_e('; '.join(items))}</li>" for label, items in
                             (("Key risks", g.key_risks), ("Review first", g.review_first),
                              ("Questions", g.questions_for_author), ("Suggested tests", g.suggested_tests)) if items)
                   + "</ul>")
    out.append("</section>")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pr_agent.dashboard", description="Render one PR Sentinel run as static HTML.")
    p.add_argument("--input", type=Path, default=Path("artifacts/run_result.json"))
    p.add_argument("--trace", type=Path, default=Path("artifacts/actions.jsonl"))
    p.add_argument("--output", type=Path, default=Path("artifacts/dashboard.html"))
    p.add_argument("--demo-data", action="store_true", help="label the page as demo data (offline fixture runs)")
    args = p.parse_args(argv)
    try:
        result = ReviewResult.model_validate_json(args.input.read_text())
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: could not read run result {args.input}: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_dashboard(result, load_trace(args.trace, result), demo=args.demo_data))
    print(f"Dashboard written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
