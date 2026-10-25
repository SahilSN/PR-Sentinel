"""Milestone 3 orchestration: triage -> gates -> decision -> (fixer) -> executor, after the review has run.

Claude only classifies and proposes. Every write decision is made here and in policy.py, deterministically.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import Limits
from .executor import Executor
from .fixer import run_fixer, validate_proposal
from .github_client import GitHubClient, GitHubError
from .policy import (AUTOFIX_TRAILER, TRUSTED_PERMISSIONS, PRFacts, decide, gate, merge_blockers, only_waiting)
from .schemas import (AutofixRecord, ChangedFile, ModelTriage, OwnerReviewGuide, PreviousReview, PullRequestInfo,
                      ReviewResult, TriageResult)
from .sentinel_config import SentinelConfig
from .tool_registry import RepoEntry
from .triage import CATEGORY_TIER, combine, deterministic_triage

WAIT_TEXT = {
    "ci_pending": "CI checks still running on {sha}",
    "ci_required_missing": "required CI check(s) {checks} to report on {sha}",
    "mergeable_unknown": "GitHub to finish computing whether the PR can merge",
    "branch_behind": "the branch to be updated from the default branch",
}


@dataclass
class Sources:
    base_text: Callable[[str], str | None] = lambda p: None
    head_text: Callable[[str], str | None] = lambda p: None
    list_repo_files: Callable[[], object] | None = None
    modes: Callable[[], dict[str, str]] = dict


@dataclass
class StateExtras:
    """Maintainer state carried in the comment's state block between runs."""

    autofix_attempts: int = 0
    failed_fix_ids: list[str] = field(default_factory=list)
    human_override: bool = False


# ---------------- facts ----------------


def facts_from_github(gh: GitHubClient, repo: str, raw: dict, config: SentinelConfig, bot_login: str,
                      default_branch: str) -> PRFacts:
    author = (raw.get("user") or {}).get("login", "")
    commits = gh.list_pr_commits(repo, raw["number"])
    permissions: dict[str, str | None] = {}

    def permission(login: str) -> str | None:
        if login not in permissions:
            permissions[login] = "bot" if login in config.trusted_bots else gh.get_permission(repo, login)
        return permissions[login]

    commit_authors, reverted, autofix_count = {}, [], 0
    for c in commits:
        message = (c.get("commit") or {}).get("message", "")
        if AUTOFIX_TRAILER in message:
            autofix_count += 1
            continue  # the agent's own autofix commits don't need a trust check
        login = (c.get("author") or {}).get("login") or (c.get("committer") or {}).get("login")
        if login:
            commit_authors[login] = permission(login)
        reverted += re.findall(r"This reverts commit ([0-9a-f]{7,40})", message)
    reviews = gh.list_reviews(repo, raw["number"])
    latest: dict[str, str] = {}
    for r in reviews:  # chronological: the last state per reviewer wins
        login = (r.get("user") or {}).get("login")
        if login and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = r["state"]
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        merges_today = gh.search_issue_count(f"repo:{repo} is:pr is:merged label:sentinel:auto-merged merged:>={since}")
        paused = bool(gh.list_issues(repo, "sentinel:paused"))
    except GitHubError:
        merges_today, paused = 10**6, True  # can't verify the caps: fail closed
    head_message = (commits[-1].get("commit") or {}).get("message", "") if commits else ""
    return PRFacts(
        head_sha=raw["head"]["sha"], state=raw.get("state", "open"), merged=bool(raw.get("merged")),
        draft=bool(raw.get("draft")), base_ref=raw["base"]["ref"], default_branch=default_branch,
        same_repo=(raw["head"].get("repo") or {}).get("full_name") == repo, title=raw.get("title") or "",
        body=raw.get("body") or "", labels=[l["name"] for l in raw.get("labels", [])], author=author,
        author_permission=permission(author) if author else None, commit_authors=commit_authors,
        agent_authored=author.lower() == bot_login.lower(), head_is_autofix=AUTOFIX_TRAILER in head_message,
        autofix_commits=autofix_count, mergeable=raw.get("mergeable"), mergeable_state=raw.get("mergeable_state"),
        changes_requested="CHANGES_REQUESTED" in latest.values(), paused=paused, merges_today=merges_today,
        head_ref=raw["head"]["ref"], commit_shas=[c["sha"] for c in commits], reverted_shas=reverted,
    )


def facts_from_fixture(data: dict, pr: PullRequestInfo) -> PRFacts:
    """Fixture key `pr_state` mirrors PRFacts; anything missing defaults to a trusted, mergeable, open PR."""
    state = {"author": "demo-dev", "author_permission": "write", "mergeable": True, "mergeable_state": "clean",
             "title": pr.title, "head_ref": "feature", **(data.get("pr_state") or {})}
    return PRFacts(head_sha=pr.head_sha, **state)


# ---------------- helpers ----------------


def _fallback_guide(tri: TriageResult, files: list[ChangedFile]) -> OwnerReviewGuide:
    forced = [s for s in tri.signals if s.source == "deterministic" and s.name not in ("size", "ast_equivalence")]
    return OwnerReviewGuide(
        change_summary=f"{tri.category} change ({tri.size}) touching {len(files)} file(s).",
        key_risks=[f"{s.name}: {s.value if not isinstance(s.value, list) else ', '.join(s.value[:3])}"
                   for s in forced[:5]],
        review_first=[f.path for f in sorted(files, key=lambda f: -(f.additions + f.deletions))[:5]],
        questions_for_author=["What is the intended behavior change, and how was it tested?"],
        suggested_tests=["Run the full test suite; add a regression test for the changed behavior."],
    )


def _model_triage_from_state(previous: PreviousReview | None) -> ModelTriage | None:
    t = previous.triage if previous else None
    if t is None or t.claude_tier is None:
        return None
    return ModelTriage(category=t.category, tier=t.claude_tier, confidence=t.confidence, rationale=t.rationale)


def _signature(tri: TriageResult | None) -> tuple:
    if tri is None:
        return ()
    return (tri.tier, tri.category, tuple(sorted({s.name for s in tri.signals if s.source == "deterministic"})))


# ---------------- the pipeline ----------------


def run_maintainer(
    *, pr: PullRequestInfo, files: list[ChangedFile], result: ReviewResult, previous: PreviousReview | None,
    facts: PRFacts, config: SentinelConfig, config_warning: str | None, autonomy: str, executor: Executor,
    model, read_source: Callable[[str], str], sources: Sources, limits: Limits, tracer,
    gate_mode: bool = False, owner_override: bool = False, force_fix: bool = False, reclassify: str | None = None,
    run_url: str | None = None,
) -> tuple[ReviewResult, StateExtras]:
    extras = StateExtras(
        autofix_attempts=max(previous.autofix_attempts if previous else 0, facts.autofix_commits),
        failed_fix_ids=list(previous.failed_fix_ids) if previous else [],
        human_override=bool(previous and previous.human_override),
    )
    notes: list[str] = [config_warning] if config_warning else []

    # 1. Triage: deterministic floor from the diff, combined with Claude's view (which can only raise it).
    tree_paths = None
    if sources.list_repo_files is not None:
        try:
            listed = sources.list_repo_files()
            entries = listed[0] if isinstance(listed, tuple) else listed
            tree_paths = {e.path for e in entries}
        except Exception:
            tree_paths = None
    det = deterministic_triage(files, result.unreviewed_files, config, title=facts.title or pr.title,
                               body=facts.body, base_text=sources.base_text, head_text=sources.head_text,
                               tree_paths=tree_paths)
    mr = result._model_review
    model_triage = mr.triage if mr is not None else _model_triage_from_state(previous)
    if mr is None and previous is not None and previous.head_sha == pr.head_sha:
        notes.append("Re-checked with fresh CI and PR data. The review text and usage are from the earlier review "
                     "of this commit (no new model call).")
    if reclassify:
        tier = CATEGORY_TIER.get(reclassify, "T2")
        model_triage = ModelTriage(category=reclassify, tier="T3" if tier == "BLOCK" else tier, confidence="high",
                                   rationale="Owner reclassified via /sentinel reclassify.")
        notes.append(f"Owner reclassified this PR as {reclassify}; the deterministic floor still applies.")
    tri = combine(det, model_triage, config)
    if mr is not None:
        tri.bugfix_assessment = mr.bugfix_assessment
        tri.owner_review_guide = mr.owner_review_guide
    elif previous and previous.triage:
        tri.bugfix_assessment = previous.triage.bugfix_assessment
        tri.owner_review_guide = previous.triage.owner_review_guide
    if tri.tier in ("T3", "BLOCK") and tri.owner_review_guide is None:
        tri.owner_review_guide = _fallback_guide(tri, files)
    tri.autonomy = autonomy
    tracer.log("triage_signals", tier=tri.tier, floor=tri.deterministic_floor, claude_tier=tri.claude_tier,
               category=tri.category, signals=[s.name for s in tri.signals])

    # 2. Humans win: an agent autofix that was reverted or rewritten makes the PR review-only for the agent.
    our_fixes = [a.model_extra.get("sha") for a in (previous.actions if previous else []) if a.type == "autofix_commit"]
    for sha in filter(None, our_fixes):
        if facts.commit_shas and (sha not in facts.commit_shas or any(sha.startswith(r) for r in facts.reverted_shas)):
            extras.human_override = True
    if extras.human_override:
        notes.append("A person reverted or rewrote PR Sentinel's autofix, so it only reviews this PR now.")

    # 3. CI still red after our autofix: no more autofix for those findings.
    if facts.head_is_autofix and result.ci and any(
            c.state == "failed" and c.name in config.merge.required_checks for c in result.ci.checks):
        last_fix = next((a for a in reversed(previous.actions if previous else []) if a.type == "autofix_commit"), None)
        ids = list((last_fix.model_extra.get("finding_ids") if last_fix else None) or [f.id for f in result.open_findings])
        extras.failed_fix_ids = sorted(set(extras.failed_fix_ids) | set(ids))
        notes.append("The autofix did not make CI pass; PR Sentinel won't try again for these findings.")

    # 4. Decide.
    attempts_left = extras.autofix_attempts < config.autofix.max_attempts_per_pr
    decision, gate_reason = gate(facts, config)
    if decision is None:
        decision = decide(tri, result, config, head_is_autofix=facts.head_is_autofix,
                          failed_fix_ids=extras.failed_fix_ids, human_override=extras.human_override,
                          attempts_left=attempts_left)
        if force_fix and tri.tier in ("T0", "T1", "T2") and result.open_findings and not det.protected \
                and attempts_left and not facts.head_is_autofix:
            decision = "fix_then_merge"
            notes.append("Owner asked for a fix via /sentinel fix.")
        if owner_override and tri.tier in ("T2", "T3") and decision in ("review_only", "suggest_fix", "escalate_owner"):
            decision = "auto_merge"
            notes.append("Owner approved merging via /sentinel merge; every other precondition still applies.")
    else:
        notes.append(gate_reason)
    if extras.failed_fix_ids and decision == "fix_then_merge":
        decision = "review_only"
    tracer.log("triage_decision", decision=decision, autonomy=autonomy, gate=gate_reason)

    # 5. Fixer: proposes; the validator decides; only the executor writes.
    record = None
    if decision in ("fix_then_merge", "suggest_fix") and model is not None and autonomy not in ("off", "review") \
            and not gate_mode:
        findings = [f for f in result.open_findings if f.id not in extras.failed_fix_ids]
        outcome = run_fixer(pr=pr, files=files, findings=findings, ci=result.ci, model=model,
                            read_source=read_source, list_repo_files=sources.list_repo_files, limits=limits,
                            config=config)
        result.stats.llm_calls += outcome.llm_calls
        result.stats.input_tokens += outcome.input_tokens
        result.stats.output_tokens += outcome.output_tokens
        plan, reason = None, outcome.rejected_reason
        if outcome.proposal is not None:
            tree = None
            if sources.list_repo_files is not None:
                try:
                    listed = sources.list_repo_files()
                    tree = {e.path: e for e in (listed[0] if isinstance(listed, tuple) else listed)}
                except Exception:
                    tree = None
            plan, reason = validate_proposal(outcome.proposal, files=files, open_findings=findings,
                                             head_text=read_source, tree=tree, modes=sources.modes(), config=config)
        attempt = extras.autofix_attempts + 1
        record = AutofixRecord(attempt=attempt, proposed=outcome.proposal is not None and outcome.proposal.can_fix,
                               rejected_reason=reason, finding_ids=plan.finding_ids if plan else [],
                               files=sorted(plan.contents) if plan else [],
                               reason=outcome.proposal.reason if outcome.proposal else "",
                               risk_notes=outcome.proposal.risk_notes if outcome.proposal else "",
                               patch_preview=plan.preview if plan else "")
        tracer.log("fix_proposed" if plan else "fix_rejected", reason=reason, files=record.files)
        if plan is None:
            notes.append(f"No autofix: {reason}.")
            decision = "review_only"
        elif decision == "fix_then_merge":
            new_sha = executor.push_autofix(branch=facts.head_ref, head_sha=result.head_sha, contents=plan.contents,
                                            message=plan.message, finding_ids=plan.finding_ids, attempt=attempt,
                                            modes=sources.modes())
            if new_sha:
                record.applied, record.commit_sha = True, new_sha
                extras.autofix_attempts = attempt
                tracer.log("fix_applied", sha=new_sha)
                executor.add_labels(["sentinel:autofixed"], facts.labels)
                executor.dispatch_ci(config.ci_workflow, facts.head_ref)
                tracer.log("ci_dispatch", workflow=config.ci_workflow)
                decision = "waiting"
                tri.waiting_for = [f"CI {', '.join(config.merge.required_checks)} on the autofix commit {new_sha[:7]}",
                                   "a fresh review of the autofix commit"]
            elif executor.errors:
                notes.append(f"Autofix not pushed: {executor.errors[-1]}")
                decision = "review_only"

    # 6. Execute the decision within the autonomy level.
    if executor.allowed("fix"):
        executor.add_labels([f"category:{tri.category}", f"size:{tri.size}"], facts.labels)
    if decision in ("escalate_owner", "block"):
        alerted = previous is not None and any(
            a.type == "request_review" or (a.type == "label" and {"sentinel:needs-owner", "sentinel:blocked"}
                                           & set((a.model_extra or {}).get("labels") or []))
            for a in previous.actions)
        unchanged = alerted and previous.decision == decision and _signature(previous.triage) == _signature(tri)
        if unchanged:
            notes.append("Owner already alerted; the reasons haven't changed.")
        else:
            executor.add_labels(["sentinel:needs-owner" if decision == "escalate_owner" else "sentinel:blocked"],
                                facts.labels)
            executor.request_review(config.owners, facts.author)
            tracer.log("escalated", decision=decision, owners=config.owners)
    if decision == "auto_merge" and autonomy == "review":
        notes.append("Review-only mode: no actions are taken, and merge preconditions weren't checked.")
    elif decision == "auto_merge":
        blockers = merge_blockers(tri, result, facts, config, autonomy, decided_for_sha=result.head_sha,
                                  owner_override=owner_override)
        real = [b for b in blockers if not b.startswith("autonomy_")]
        tracer.log("merge_gate", blockers=real)
        if not real:
            def after_merge():
                executor.add_labels(["sentinel:auto-merged"], facts.labels)
                # A merge made with GITHUB_TOKEN doesn't start push workflows, so CI on the default branch (and the
                # post-merge guard that listens for it) would never run. Start it explicitly.
                executor.dispatch_ci(config.ci_workflow, facts.default_branch)

            status, detail, fresh = executor.locked_merge(
                tree_sha=_tree_of(executor, facts.head_sha), recheck=lambda: fresh_merge_blockers(executor, config),
                after_merge=after_merge, head_sha=facts.head_sha, title=facts.title or pr.title,
                method=config.merge.method, category=tri.category, tier=tri.tier,
                checks=config.merge.required_checks, run_url=run_url)
            tracer.log("merge_result", status=status, detail=detail)
            if status == "locked":
                decision, tri.waiting_for = "waiting", ["another PR Sentinel merge to finish (repository-wide merge lock)"]
                notes.append("Another merge held the lock; this PR is re-checked on its next event (CI, push or label).")
            elif status == "blocked":
                decision = "review_only"
                real = real + fresh
            elif status == "not_mergeable":
                decision, tri.waiting_for = "waiting", ["GitHub to report the PR as mergeable"]
            elif status in ("stale", "error"):
                decision = "review_only"
                notes.append(f"Merge not done ({status}): {detail}. It will be re-evaluated on the next event.")
        elif only_waiting(real):
            decision = "waiting"
            tri.waiting_for = [WAIT_TEXT[b].format(sha=facts.head_sha[:7], checks=", ".join(config.merge.required_checks))
                               for b in real]
            if "branch_behind" in real:
                executor.update_branch(facts.head_sha)
        else:
            decision = "review_only"
        tri.blocked_by = real

    tri.decision = decision
    reused = mr is None and previous is not None and previous.head_sha == pr.head_sha
    if reused and record is None and decision in ("fix_then_merge", "suggest_fix") and previous.autofix is not None:
        record = previous.autofix  # the fixer only runs on a fresh review; keep what that review proposed
        carried = [w for w in (previous.triage.would_have or []) if "autofix" in w] if previous.triage else []
        executor.would_have = carried + executor.would_have
    tri.would_have = executor.would_have or None
    tri.notes = notes + [f"Write failed: {e}" for e in executor.errors]
    result.triage = tri
    result.autofix = record
    result.actions = executor.actions
    return result, extras


def count_recent_auto_merges(pulls: list[dict], now: datetime | None = None) -> int:
    """Autonomous merges in the last 24 hours, from the pulls list (consistent; search can lag behind)."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=1)
    count = 0
    for p in pulls:
        merged = p.get("merged_at")
        if merged and "sentinel:auto-merged" in [l.get("name") for l in p.get("labels", [])]:
            if datetime.fromisoformat(merged.replace("Z", "+00:00")) >= since:
                count += 1
    return count


def fresh_merge_blockers(executor: Executor, config: SentinelConfig) -> list[str]:
    """Re-checked while holding the merge lock, so two PRs can't both pass the daily cap or ignore a new pause."""
    gh, repo = executor.gh, executor.repo
    blockers = []
    if count_recent_auto_merges(gh.recent_closed_pulls(repo)) >= config.merge.max_auto_merges_per_day:
        blockers.append("daily_merge_cap")
    if gh.list_issues(repo, "sentinel:paused"):
        blockers.append("sentinel_paused")
    return blockers


def _tree_of(executor: Executor, sha: str) -> str:
    """Any existing tree works for the (empty-change) lock commit; use the PR head's."""
    if executor.gh is None or not executor.allowed("merge"):
        return ""
    return executor.gh.get_git_commit(executor.repo, sha)["tree"]["sha"]


def trusted(facts: PRFacts, config: SentinelConfig) -> bool:
    return facts.author in config.trusted_bots or facts.author_permission in TRUSTED_PERMISSIONS


__all__ = ["Sources", "StateExtras", "facts_from_fixture", "facts_from_github", "run_maintainer", "RepoEntry"]
