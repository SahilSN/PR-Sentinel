"""Milestone 3 decision engine: pure functions over (triage, review, GitHub facts, config). No I/O here.

Claude never decides to write; this module does, deterministically, and every merge precondition that fails is
recorded in `blocked_by`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schemas import CIReport, Finding, ReviewResult, TriageResult
from .sentinel_config import SentinelConfig
from .triage import TIERS, bugfix_gaps

TRUSTED_PERMISSIONS = {"admin", "maintain", "write"}
AUTOFIX_TRAILER = "PR-Sentinel-Autofix:"
MERGE_TRAILER = "PR-Sentinel-Merge:"
WAIT_BLOCKERS = {"ci_pending", "ci_required_missing", "mergeable_unknown", "branch_behind"}


@dataclass
class PRFacts:
    """Fresh facts about the PR from GitHub (or a fixture). Everything here is checked at decision time."""

    head_sha: str
    state: str = "open"
    merged: bool = False
    draft: bool = False
    base_ref: str = "main"
    default_branch: str = "main"
    same_repo: bool = True
    title: str = ""
    labels: list[str] = field(default_factory=list)
    author: str = ""
    author_permission: str | None = None  # admin | maintain | write | triage | read | none
    commit_authors: dict[str, str | None] = field(default_factory=dict)  # login -> permission
    agent_authored: bool = False  # opened by PR Sentinel itself (revert / issue-fix PRs)
    head_is_autofix: bool = False
    autofix_commits: int = 0  # commits on the PR carrying the autofix trailer
    mergeable: bool | None = None
    mergeable_state: str | None = None
    changes_requested: bool = False
    paused: bool = False
    merges_today: int = 0
    body: str = ""
    head_ref: str = ""
    commit_shas: list[str] = field(default_factory=list)
    reverted_shas: list[str] = field(default_factory=list)


# ---------------- gates ----------------


def hold_reason(facts: PRFacts, config: SentinelConfig) -> str | None:
    if {"sentinel:hold", "do-not-merge"} & {l.lower() for l in facts.labels}:
        return "hold label"
    title = facts.title.lower()
    for marker in config.hold_markers:
        if re.search(r"(?<![a-z])" + re.escape(marker.lower()) + r"(?![a-z])", title):
            return f"title marker {marker!r}"
    return None


def gate(facts: PRFacts, config: SentinelConfig) -> tuple[str | None, str | None]:
    """(decision, reason) when a PR-state gate decides the outcome before triage, else (None, None)."""
    if facts.state != "open" or facts.merged:
        return "skip", "PR is closed or merged"
    reason = hold_reason(facts, config)
    if reason:
        return "hold", f"On hold ({reason})"
    if facts.draft:
        return "review_only", "Draft PR: review only until it's ready for review"
    if not facts.same_repo:
        return "review_only", "Head branch is in another repository: review only"
    if facts.base_ref != facts.default_branch:
        return "review_only", f"Base branch is {facts.base_ref!r}, not the default branch: review only"
    return None, None


# ---------------- decision matrix (MILESTONE_3.md §5.3) ----------------


def blocking_findings(findings: list[Finding], tier: str) -> list[Finding]:
    if tier == "T0":
        return list(findings)
    return [f for f in findings if f.severity in ("medium", "high")]


def decide(triage: TriageResult, review: ReviewResult, config: SentinelConfig, *, fixable: bool | None = None,
           head_is_autofix: bool = False, failed_fix_ids: list[str] | None = None,
           human_override: bool = False, attempts_left: bool = True) -> str:
    """Base decision before merge preconditions. `fixable` is None until the fixer has been asked."""
    tier = triage.tier
    if tier == "BLOCK":
        return "block"
    if tier == "T3":
        return "escalate_owner"
    if review.status != "completed":
        return "review_only"
    findings = review.open_findings
    stuck = set(failed_fix_ids or [])
    fix_allowed = (tier in config.autofix.allowed_tiers and not head_is_autofix and not human_override
                   and attempts_left and not any(f.id in stuck for f in findings))
    override = config.categories.get(triage.category)

    if tier == "T2":
        if override in ("review_only", "escalate_owner"):
            return override
        return "suggest_fix" if findings and fixable is not False else "review_only"
    # T0 / T1
    if override in ("review_only", "suggest_fix", "escalate_owner"):
        return override
    if findings:
        if fix_allowed and fixable is not False:
            return "fix_then_merge"
        return "review_only"
    if tier == "T1" and bugfix_gaps(triage, triage.bugfix_assessment):
        return "review_only"
    return "auto_merge"


# ---------------- merge preconditions (MILESTONE_3.md §5.4) ----------------


def ci_blockers(ci: CIReport | None, config: SentinelConfig, docs_only: bool) -> list[str]:
    if ci is None or ci.state in ("unavailable", "not_found"):
        if docs_only and config.merge.allow_docs_without_ci and (ci is None or ci.state == "not_found"):
            return []
        return [f"ci_{ci.state if ci else 'unavailable'}"]
    blockers = []
    by_name = {c.name: c for c in ci.checks}
    for name in config.merge.required_checks:
        check = by_name.get(name)
        if check is None:
            blockers.append("ci_required_missing")  # not reported yet: wait for it
        elif check.state == "pending":
            blockers.append("ci_pending")
        elif check.state != "passed":
            blockers.append(f"ci_failed:{name}")
    for c in ci.checks:
        if c.name not in config.merge.required_checks:
            if c.state == "failed":
                blockers.append(f"ci_failed:{c.name}")
            elif c.state == "pending":
                blockers.append("ci_pending")
    return sorted(set(blockers))


def merge_blockers(triage: TriageResult, review: ReviewResult, facts: PRFacts, config: SentinelConfig,
                   autonomy: str, *, decided_for_sha: str, owner_override: bool = False) -> list[str]:
    """Every failing §5.4 precondition. An empty list means the PR may be merged now."""
    b: list[str] = []
    if autonomy != "merge":
        b.append(f"autonomy_{autonomy}")  # the only blocker allowed to start with "autonomy_"
    if triage.tier not in config.merge.allowed_tiers and not (owner_override and triage.tier in ("T2", "T3")):
        b.append(f"tier_{triage.tier}_not_allowed")
    if decided_for_sha != facts.head_sha:
        b.append("stale_head_sha")
    if facts.state != "open" or facts.merged:
        b.append("pr_not_open")
    if facts.draft:
        b.append("draft")
    if facts.base_ref != facts.default_branch:
        b.append("base_not_default_branch")
    if not facts.same_repo:
        b.append("fork")
    if hold_reason(facts, config):
        b.append("on_hold")
    if facts.paused:
        b.append("sentinel_paused")  # not "autonomy_*": those mean "autonomy level too low" and are filtered
    trusted = set(config.trusted_bots)
    if facts.author not in trusted and facts.author_permission not in TRUSTED_PERMISSIONS:
        b.append("untrusted_author")
    for login, permission in facts.commit_authors.items():
        if login not in trusted and permission not in TRUSTED_PERMISSIONS:
            b.append(f"untrusted_commit_author:{login}")
    if facts.agent_authored and config.require_owner_for_agent_prs and not owner_override:
        b.append("agent_authored_pr")
    protected = next((s.value for s in triage.signals if s.name == "protected_paths"), None)
    if protected and not (owner_override and config.merge.owner_can_merge_protected):
        b.append("protected_paths")
    if any(s.name in ("secret", "hidden_unicode", "encoded_exec", "minified_code") for s in triage.signals):
        b.append("block_signal")
    if review.unreviewed_files:
        b.append("unreviewed_files")
    if review.status != "completed":
        b.append(f"review_{review.status}")
    if blocking_findings(review.open_findings, triage.tier):
        b.append("open_findings")
    if triage.confidence != "high" and not owner_override:
        b.append("low_triage_confidence")
    for gap in bugfix_gaps(triage, triage.bugfix_assessment):
        if not owner_override:
            b.append(f"bugfix:{gap}")
    docs_only = triage.tier == "T0" and triage.category == "docs"
    b += ci_blockers(review.ci, config, docs_only)
    if facts.mergeable is None or facts.mergeable_state in (None, "unknown"):
        b.append("mergeable_unknown")
    elif facts.mergeable_state == "behind":
        b.append("branch_behind")
    elif not facts.mergeable or facts.mergeable_state not in ("clean", "has_hooks", "unstable"):
        b.append(f"not_mergeable:{facts.mergeable_state}")
    if facts.changes_requested:
        b.append("changes_requested")
    if facts.merges_today >= config.merge.max_auto_merges_per_day:
        b.append("daily_merge_cap")
    return b


def only_waiting(blockers: list[str]) -> bool:
    """True when the only obstacles are things that resolve by themselves (CI running, GitHub computing)."""
    return bool(blockers) and all(x in WAIT_BLOCKERS for x in blockers)


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)
