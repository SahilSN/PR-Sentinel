"""Post-merge guard: when CI fails on the default branch after an AUTONOMOUS merge, open a revert PR and pause.

    python -m pr_agent.guard --repo OWNER/REPO --sha MAIN_COMMIT --conclusion failure [--run-url URL] [--publish]

The revert restores each file the merge changed to its content before the merge. If any of those files changed
again on the default branch since, it doesn't build a revert (that could undo someone else's work); it escalates.
Either way it opens a `sentinel:paused` issue: merges stay off until a person closes it.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

from . import config
from .executor import LABEL_COLORS
from .github_client import GitHubClient, GitHubError
from .policy import MERGE_TRAILER


@dataclass
class GuardOutcome:
    action: str  # ignored | reverted | escalated | dry_run
    detail: str = ""
    writes: list[str] = field(default_factory=list)


def revert_entries(gh: GitHubClient, repo: str, merge_sha: str, parent_sha: str, files: list[dict]) -> list[dict]:
    parent_tree = {e["path"]: e for e in gh.get_tree(repo, parent_sha)[0] if e.get("type") == "blob"}
    entries = []
    for f in files:
        path, status = f["filename"], f.get("status")
        if status == "renamed" and f.get("previous_filename"):
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            path = f["previous_filename"]
        old = parent_tree.get(path)
        if old is None:  # the merge added it: delete
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            entries.append({"path": path, "mode": old.get("mode", "100644"), "type": "blob", "sha": old["sha"]})
    return entries


def touched_paths(files: list[dict]) -> list[str]:
    """Every path a revert would write: a rename's old path is restored too, so it must be checked as well
    (QA report docs/qa/2026-10-24-milestone-3.md)."""
    paths = []
    for f in files:
        paths.append(f["filename"])
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])
    return paths


def changed_since(gh: GitHubClient, repo: str, merge_sha: str, head_sha: str, paths: list[str]) -> list[str]:
    if head_sha == merge_sha:
        return []
    at_merge = {e["path"]: e.get("sha") for e in gh.get_tree(repo, merge_sha)[0]}
    at_head = {e["path"]: e.get("sha") for e in gh.get_tree(repo, head_sha)[0]}
    return [p for p in paths if at_merge.get(p) != at_head.get(p)]


def run_guard(gh: GitHubClient, repo: str, sha: str, conclusion: str, run_url: str | None,
              publish: bool) -> GuardOutcome:
    if conclusion != "failure":
        return GuardOutcome("ignored", f"CI concluded {conclusion}")
    commit = gh.get_commit(repo, sha)
    message = (commit.get("commit") or {}).get("message", "")
    if MERGE_TRAILER not in message:
        return GuardOutcome("ignored", "not an autonomous merge")
    m = re.search(r"\(#(\d+)\)", message.splitlines()[0] if message else "")
    pr_number = int(m.group(1)) if m else None
    parent = commit["parents"][0]["sha"]
    files = commit.get("files", [])
    default_branch = gh.get_repo(repo).get("default_branch", "main")
    main_head = gh.get_ref_sha(repo, default_branch)
    later = changed_since(gh, repo, sha, main_head, touched_paths(files))
    link = f" ([failing run]({run_url}))" if run_url and run_url.startswith("https://github.com/") else ""
    out = GuardOutcome("escalated" if later else "reverted")
    if not publish:
        out.action, out.detail = "dry_run", ("would escalate" if later else "would open a revert PR") + " and pause"
        return out

    def label(*names):
        for n in names:
            gh.ensure_label(repo, n, LABEL_COLORS.get(n, "c5def5"))

    revert_pr = None
    if not later:
        entries = revert_entries(gh, repo, sha, parent, files)
        base_tree = gh.get_git_commit(repo, main_head)["tree"]["sha"]
        tree = gh.create_tree(repo, base_tree, entries)
        title = f"Revert #{pr_number}: main CI failing after autonomous merge" if pr_number else \
            f"Revert {sha[:7]}: main CI failing after autonomous merge"
        new_commit = gh.create_commit(repo, f"{title}\n\nThis reverts commit {sha}.", tree, [main_head])
        branch = f"sentinel/revert-{sha[:7]}"
        gh.create_ref(repo, branch, new_commit)
        revert_pr = gh.create_pull(repo, branch, default_branch, title,
                                   f"CI failed on `{default_branch}` after PR Sentinel merged {sha[:7]}{link}.\n\n"
                                   "This PR restores the files that merge changed. A person should review and merge it.")
        label("sentinel:revert", "sentinel:needs-owner")
        gh.add_labels(repo, revert_pr["number"], ["sentinel:revert", "sentinel:needs-owner"])
        out.writes.append(f"revert PR #{revert_pr['number']}")
        out.detail = f"opened revert PR #{revert_pr['number']}"
    else:
        out.detail = "files changed again since the merge: " + ", ".join(later[:5])
    if pr_number:
        note = (f"Revert PR #{revert_pr['number']} opened." if revert_pr else
                "No automatic revert: those files changed again since. Please investigate.")
        gh.create_comment(repo, pr_number, f"CI failed on `{default_branch}` after PR Sentinel merged this PR{link}. "
                                           f"{note} Autonomous merges are paused.")
        out.writes.append(f"comment on #{pr_number}")
    if not gh.list_issues(repo, "sentinel:paused"):
        label("sentinel:paused")
        issue = gh.create_issue(repo, "PR Sentinel paused: main CI failed after an autonomous merge",
                                f"Commit {sha[:7]} (from #{pr_number}) broke CI on `{default_branch}`{link}.\n\n"
                                "Autonomous merges are disabled while this issue is open. Close it to resume.",
                                ["sentinel:paused"])
        out.writes.append(f"pause issue #{issue['number']}")
    return out


def main(argv: list[str] | None = None, gh: GitHubClient | None = None) -> int:
    p = argparse.ArgumentParser(prog="pr_agent.guard")
    p.add_argument("--repo", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--conclusion", required=True)
    p.add_argument("--run-url")
    p.add_argument("--publish", action="store_true")
    args = p.parse_args(argv)
    gh = gh or GitHubClient(config.github_token() or "", read_only=not args.publish)
    try:
        out = run_guard(gh, args.repo, args.sha, args.conclusion, args.run_url, args.publish)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"guard: {out.action}. {out.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
