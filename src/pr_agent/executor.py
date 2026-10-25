"""Milestone 3 executor: the only code that writes to GitHub on PR Sentinel's behalf (besides the review comment).

Every write is SHA-pinned and idempotent where GitHub allows it. In `review`/`shadow` autonomy or a dry run,
nothing is written: the intended action is recorded as "would have" instead.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .github_client import GitHubClient, GitHubError
from .policy import AUTOFIX_TRAILER, MERGE_TRAILER
from .schemas import Action
from .tool_registry import RepoEntry

# Repository-wide merge lock (MILESTONE_3.md §7 "serialize merges"): a git ref outside refs/heads, so it's not a
# branch and pushes nothing. Creating it is atomic; a crashed holder's lock expires after LOCK_TTL_SECONDS.
LOCK_REF = "sentinel/merge-lock"
LOCK_TTL_SECONDS = 600
LOCK_WAIT_SECONDS = 90  # merges take seconds, so a short wait in the job beats stalling until the next event
LOCK_POLL_SECONDS = 3

LABEL_COLORS = {
    "sentinel:hold": "d4c5f9", "sentinel:needs-owner": "fbca04", "sentinel:blocked": "b60205",
    "sentinel:auto-merged": "0e8a16", "sentinel:autofixed": "1d76db", "sentinel:paused": "b60205",
    "sentinel:revert": "d93f0b",
}


@dataclass
class Executor:
    gh: GitHubClient | None
    repo: str
    pr_number: int
    autonomy: str
    can_write: bool  # --publish and not --dry-run
    run_id: str
    actions: list[Action] = field(default_factory=list)
    would_have: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time

    def allowed(self, needs: str) -> bool:
        """`needs` is the minimum autonomy for the action: 'fix' (push/label/escalate) or 'merge'."""
        order = ["off", "review", "shadow", "fix", "merge"]
        return self.can_write and self.gh is not None and order.index(self.autonomy) >= order.index(needs)

    def act(self, needs: str, description: str, fn, record: dict) -> bool:
        """Run `fn` if allowed; otherwise record what would have happened. Returns True if it ran successfully."""
        if self.autonomy in ("off", "review"):
            return False
        if not self.allowed(needs):
            suffix = "" if self.autonomy == "shadow" else " (not done: dry run or autonomy too low)"
            self.would_have.append(description + suffix)
            return False
        try:
            extra = fn() or {}
        except GitHubError as exc:
            self.errors.append(f"{description}: {exc}")
            return False
        self.actions.append(Action(**{**record, **extra}))
        return True

    # ---------- labels / escalation ----------

    def add_labels(self, labels: list[str], present: list[str]) -> None:
        missing = [l for l in labels if l not in present]
        if not missing:
            return

        def run():
            for label in missing:
                self.gh.ensure_label(self.repo, label, LABEL_COLORS.get(label, "c5def5"))
            self.gh.add_labels(self.repo, self.pr_number, missing)

        self.act("fix", f"add labels {', '.join(missing)}", run, {"type": "label", "labels": missing})

    def request_review(self, owners: list[str], author: str) -> None:
        reviewers = [o for o in owners if o.lower() != author.lower()]  # GitHub rejects requests to the PR author
        if reviewers:
            self.act("fix", f"request review from {', '.join(reviewers)}",
                     lambda: self.gh.request_reviewers(self.repo, self.pr_number, reviewers),
                     {"type": "request_review", "reviewers": reviewers})

    # ---------- autofix ----------

    def push_autofix(self, *, branch: str, head_sha: str, contents: dict[str, str], message: str,
                     finding_ids: list[str], attempt: int, modes: dict[str, str]) -> str | None:
        """One atomic commit via the Git Data API, fast-forward only. Returns the new commit SHA."""
        new_sha: dict[str, str] = {}

        def run():
            if self.gh.get_ref_sha(self.repo, branch) != head_sha:
                raise GitHubError(None, "branch moved since the review; autofix aborted")
            base_tree = self.gh.get_git_commit(self.repo, head_sha)["tree"]["sha"]
            entries = [{"path": p, "mode": modes.get(p, "100644"), "type": "blob", "content": text}
                       for p, text in sorted(contents.items())]
            tree = self.gh.create_tree(self.repo, base_tree, entries)
            body = (f"{message}\n\n{AUTOFIX_TRAILER} run={self.run_id} attempt={attempt}\n"
                    f"Fixes-Findings: {', '.join(finding_ids)}")
            commit = self.gh.create_commit(self.repo, body, tree, [head_sha])
            try:
                self.gh.update_ref(self.repo, branch, commit)  # force: false
            except GitHubError as exc:
                if exc.status == 422:
                    raise GitHubError(422, "branch moved during the autofix; not forcing") from None
                raise
            new_sha["sha"] = commit
            return {"sha": commit, "files": sorted(contents), "finding_ids": finding_ids, "attempt": attempt}

        ok = self.act("fix", f"push an autofix commit to {branch} ({len(contents)} file(s))", run,
                      {"type": "autofix_commit"})
        return new_sha.get("sha") if ok else None

    def dispatch_ci(self, workflow: str, branch: str) -> None:
        self.act("fix", f"start CI workflow {workflow} on {branch}",
                 lambda: self.gh.dispatch_workflow(self.repo, workflow, branch),
                 {"type": "ci_dispatch", "workflow": workflow, "ref": branch})

    # ---------- merge ----------

    def merge(self, *, head_sha: str, title: str, method: str, category: str, tier: str, checks: list[str],
              run_url: str | None) -> tuple[str, str | None]:
        """('merged' | 'not_mergeable' | 'stale' | 'error' | 'skipped', detail)."""
        clean_title = re.sub(r"@(?=\w)", "", " ".join(title.split()))[:200]
        message = (f"Merged autonomously by PR Sentinel.\n\nCategory: {category}, tier: {tier}.\n"
                   f"Checks passed: {', '.join(checks) or 'none required'}.\n"
                   + (f"Run: {run_url}\n" if run_url else "") + f"\n{MERGE_TRAILER} run={self.run_id}")
        outcome: dict[str, str | None] = {"status": "skipped", "detail": None}

        def run():
            try:
                data = self.gh.merge_pull(self.repo, self.pr_number, head_sha, method,
                                          f"{clean_title} (#{self.pr_number})", message)
            except GitHubError as exc:
                outcome["status"] = {405: "not_mergeable", 409: "stale"}.get(exc.status, "error")
                outcome["detail"] = str(exc)
                raise
            outcome["status"] = "merged"
            return {"merge_sha": data.get("sha"), "method": method, "head_sha": head_sha}

        self.act("merge", f"merge #{self.pr_number} at {head_sha[:7]} ({method})", run, {"type": "merge"})
        return outcome["status"], outcome["detail"]

    # ---------- repository-wide merge lock ----------

    def _lock_age(self, sha: str) -> float:
        committed = self.gh.get_git_commit(self.repo, sha).get("committer", {}).get("date")
        try:
            when = datetime.fromisoformat(committed.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            return float("inf")  # unreadable lock: treat as stale
        return self.clock() - when

    def acquire_merge_lock(self, tree_sha: str) -> str | None:
        """The lock commit's SHA once held, or None if another run held it for the whole wait."""
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = f"PR Sentinel merge lock\n\nrun={self.run_id} pr=#{self.pr_number}"
        mine = self.gh.create_commit(self.repo, message, tree_sha, [])
        deadline = self.clock() + LOCK_WAIT_SECONDS
        while True:
            try:
                self.gh.create_full_ref(self.repo, LOCK_REF, mine)
                return mine
            except GitHubError as exc:
                if exc.status != 422:
                    raise
            held = self.gh.get_ref(self.repo, LOCK_REF)
            if held and self._lock_age(held) > LOCK_TTL_SECONDS:
                # Stale (its holder crashed). Take it over with a fast-forward: if another run got there first,
                # the ref no longer points at `held`, the update isn't a fast-forward, and GitHub refuses it.
                takeover = self.gh.create_commit(self.repo, message + f"\ntook over stale lock {held[:7]} at {now}",
                                                 tree_sha, [held])
                try:
                    self.gh.fast_forward_ref(self.repo, LOCK_REF, takeover)
                    return takeover
                except GitHubError as exc:
                    if exc.status != 422:
                        raise
            if self.clock() >= deadline:
                return None
            self.sleep(LOCK_POLL_SECONDS)

    def release_merge_lock(self, sha: str) -> None:
        """Only release our own lock: if a stale-lock takeover replaced it, it isn't ours anymore."""
        try:
            if self.gh.get_ref(self.repo, LOCK_REF) == sha:
                self.gh.delete_ref(self.repo, LOCK_REF)
        except GitHubError as exc:
            self.errors.append(f"release merge lock: {exc}")

    def locked_merge(self, *, tree_sha: str, recheck: Callable[[], list[str]], after_merge: Callable[[], None],
                     **merge_args) -> tuple[str, str | None, list[str]]:
        """Merge while holding the repository-wide lock: re-check fresh facts under the lock, merge, then run
        `after_merge` (label, CI dispatch) before releasing. Returns (status, detail, fresh blockers)."""
        if not self.allowed("merge"):
            status, detail = self.merge(**merge_args)  # records "would have"
            return status, detail, []
        try:
            lock = self.acquire_merge_lock(tree_sha)
        except GitHubError as exc:
            self.errors.append(f"merge lock: {exc}")
            return "error", str(exc), []
        if lock is None:
            return "locked", "another PR Sentinel merge held the lock", []
        try:
            blockers = recheck()
            if blockers:
                return "blocked", ", ".join(blockers), blockers
            status, detail = self.merge(**merge_args)
            if status == "merged":
                after_merge()
            return status, detail, []
        finally:
            self.release_merge_lock(lock)

    def update_branch(self, head_sha: str) -> None:
        self.act("merge", "update the branch from the default branch",
                 lambda: self.gh.update_branch(self.repo, self.pr_number, head_sha),
                 {"type": "update_branch", "expected_head_sha": head_sha})


def tree_modes(gh: GitHubClient | None, repo: str, sha: str, entries: list[RepoEntry] | None = None) -> dict[str, str]:
    """path -> git file mode at `sha`, so an autofix keeps executables executable and never follows symlinks."""
    if gh is None:
        return {}
    try:
        tree, _ = gh.get_tree(repo, sha)
    except GitHubError:
        return {}
    return {e["path"]: e.get("mode", "100644") for e in tree if e.get("type") == "blob"}
