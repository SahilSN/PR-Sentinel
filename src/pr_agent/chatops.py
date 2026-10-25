"""ChatOps: `/sentinel <command>` in a PR comment, from someone with write access.

    python -m pr_agent.chatops --repo OWNER/REPO --pr N --comment-id ID [--publish]

Only the first line of the comment is parsed. Anyone without write/maintain/admin is ignored silently. This never
checks out PR code: it runs the normal pipeline, which only reads the PR through the API.
"""

from __future__ import annotations

import argparse
import re
import sys

from . import cli, config
from .executor import LABEL_COLORS
from .github_client import GitHubClient, GitHubError
from .policy import TRUSTED_PERMISSIONS
from .review import _escape
from .schemas import CATEGORIES
from .state import find_bot_comment, decode_state, resolve_bot_login

COMMAND = re.compile(r"^/sentinel (\w+)(?: (\S+))?$")
COMMANDS = {"hold", "release", "merge", "fix", "recheck", "explain", "reclassify"}


def parse_command(body: str) -> tuple[str, str | None] | None:
    first = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    m = COMMAND.match(first)
    if not m or m.group(1) not in COMMANDS:
        return None
    return m.group(1), m.group(2)


def explain(gh: GitHubClient, repo: str, pr: int, bot_login: str) -> str:
    comment = find_bot_comment(gh.list_issue_comments(repo, pr), bot_login)
    state = decode_state(comment["body"]) if comment else None
    if state is None or state.triage is None:
        return "PR Sentinel has no triage recorded for this PR yet."
    t = state.triage
    lines = [f"**Decision trace for `{state.head_sha[:7]}`:** {t.decision} (autonomy `{t.autonomy}`), tier {t.tier} "
             f"(floor {t.deterministic_floor}, Claude {t.claude_tier or '-'}), category {_escape(t.category)}.", "",
             "| Signal | Value | Source |", "|---|---|---|"]
    for s in t.signals:
        value = ", ".join(map(str, s.value[:5])) if isinstance(s.value, list) else str(s.value)
        lines.append(f"| {_escape(s.name, single_line=True)} | {_escape(value, single_line=True, limit=120)} | {s.source} |")
    if t.blocked_by:
        lines += ["", "Blocked by: " + _escape(", ".join(t.blocked_by), single_line=True)]
    return "\n".join(lines)


def main(argv: list[str] | None = None, gh: GitHubClient | None = None, model=None) -> int:
    p = argparse.ArgumentParser(prog="pr_agent.chatops")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--comment-id", type=int, required=True)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--fake-model", action="store_true")
    p.add_argument("--output-dir", default="artifacts")
    p.add_argument("--follow-ci", action="store_true", help="wait for CI this run starts itself, then carry on")
    args = p.parse_args(argv)
    gh = gh or GitHubClient(config.github_token() or "", read_only=not args.publish)
    try:
        comment = gh.get_issue_comment(args.repo, args.comment_id)
        parsed = parse_command(comment.get("body", ""))
        if parsed is None:
            return 0
        user = (comment.get("user") or {}).get("login", "")
        if gh.get_permission(args.repo, user) not in TRUSTED_PERMISSIONS:
            return 0  # silently ignore people without write access
        command, arg = parsed
        if args.publish:
            gh.add_reaction(args.repo, args.comment_id, "eyes")
        base = ["--repo", args.repo, "--pr", str(args.pr), "--output-dir", args.output_dir,
                "--publish" if args.publish else "--dry-run"] + (["--fake-model"] if args.fake_model else []) \
            + (["--follow-ci"] if args.follow_ci else [])
        if command == "hold":
            if args.publish:
                gh.ensure_label(args.repo, "sentinel:hold", LABEL_COLORS["sentinel:hold"])
                gh.add_labels(args.repo, args.pr, ["sentinel:hold"])
            return 0
        if command == "release":
            if args.publish:
                gh.remove_label(args.repo, args.pr, "sentinel:hold")
            return cli.main(base, gh=gh, model=model)  # label events from GITHUB_TOKEN don't re-trigger workflows
        if command == "explain":
            if args.publish:
                gh.create_comment(args.repo, args.pr,
                                  explain(gh, args.repo, args.pr, resolve_bot_login(gh, None)))
            return 0
        if command == "merge":
            return cli.main(base + ["--owner-merge"], gh=gh, model=model)
        if command == "fix":
            return cli.main(base + ["--force-fix", "--force"], gh=gh, model=model)
        if command == "recheck":
            return cli.main(base + ["--force"], gh=gh, model=model)
        if command == "reclassify":
            if arg not in CATEGORIES:
                return 0
            return cli.main(base + ["--reclassify", arg], gh=gh, model=model)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
