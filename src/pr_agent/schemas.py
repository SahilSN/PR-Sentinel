"""Shared data contracts. Teammates build against these - coordinate before changing names or types.

Input:  PullRequestInfo + list[ChangedFile] + PreviousReview | None   (PROJECT.md 6.3)
Output: ReviewResult, written to artifacts/run_result.json            (PROJECT.md 6.4)
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

SCHEMA_VERSION = 1

Status = Literal["completed", "needs_human", "budget_exceeded", "error"]
Level = Literal["low", "medium", "high"]
FileStatus = Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]


# ---------- Input ----------


class ChangedFile(BaseModel):
    path: str
    status: FileStatus = "modified"
    patch: str | None = None  # None when GitHub omits it (binary, too large): such files are unreviewed, never "clean"
    additions: int = 0
    deletions: int = 0


class PullRequestInfo(BaseModel):
    repo: str
    pr_number: int = Field(gt=0)
    title: str = ""
    base_sha: str
    head_sha: str

    @field_validator("repo")
    @classmethod
    def _repo_format(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
            raise ValueError("repo must look like OWNER/REPO")
        return v


# ---------- Output ----------


class Finding(BaseModel):
    id: str  # deterministic "<path>:<kind>" - stable across commits, not tied to the line number
    path: str
    line: int | None = None  # only set when grounded in a known line of the PR diff
    severity: Level
    title: str
    explanation: str
    suggestion: str
    confidence: Level
    times_seen: int = 1  # additive field: how many distinct head SHAs have reported this finding


CheckState = Literal["passed", "failed", "pending", "neutral"]
# passed: ≥1 check passed, none failed/pending · failed: ≥1 failed, none pending · pending: still running, none failed
# mixed: ≥1 failed while others still run · not_found: no usable checks · unavailable: could not read checks
CIState = Literal["passed", "failed", "pending", "mixed", "unavailable", "not_found"]


class CheckAnnotation(BaseModel):
    path: str
    line: int | None = None
    level: str = "failure"
    message: str


class CheckRun(BaseModel):
    """One GitHub check run for the reviewed head SHA. Everything here is observed data from GitHub."""

    id: int
    name: str
    state: CheckState
    status: str  # raw GitHub status: queued | in_progress | completed | ...
    conclusion: str | None = None  # raw GitHub conclusion when completed
    url: str | None = None
    summary: str = ""
    annotations: list[CheckAnnotation] = Field(default_factory=list)
    truncated: bool = False


class CIReport(BaseModel):
    """Optional `ci` block of run_result.json (Milestone 2). Observed facts plus a clearly separate interpretation."""

    head_sha: str
    state: CIState
    checks: list[CheckRun] = Field(default_factory=list)
    ignored_checks: list[str] = Field(default_factory=list)  # e.g. PR Sentinel's own check, or other commits' checks
    truncated: bool = False
    error: str | None = None  # why state is "unavailable"
    likely_cause: str | None = None  # Claude's interpretation, only when CI failed and evidence supports it
    recommended_next_step: str | None = None


EscalationCode = Literal[
    "INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "TOOL_FAILURES", "REPEATED_TOOL_REQUEST",
    "INVALID_TOOL_REQUESTS", "REVIEW_SCHEMA_INVALID", "MODEL_DECLINED", "MODEL_REQUESTED", "LIMIT_REACHED",
    "NOTHING_REVIEWABLE",
]


class Escalation(BaseModel):
    """Optional `escalation` block: why the run stopped short of `completed`, and what a person should do."""

    reason_code: EscalationCode
    reason: str
    tried: list[str] = Field(default_factory=list)
    unknown: str = ""
    next_step: str = ""


class ContextFile(BaseModel):
    """A file the agent actually read, at the pinned head SHA."""

    path: str
    sha: str
    via: Literal["changed", "related"]
    chars: int
    truncated: bool = False


# ---------- Milestone 3: triage, decisions, autofix, actions ----------

Tier = Literal["T0", "T1", "T2", "T3", "BLOCK"]
ModelTier = Literal["T0", "T1", "T2", "T3"]
Decision = Literal["skip", "hold", "review_only", "auto_merge", "fix_then_merge", "suggest_fix", "escalate_owner",
                   "block", "waiting"]
Autonomy = Literal["off", "review", "shadow", "fix", "merge"]
CATEGORIES = [
    "docs", "comments_only", "formatting", "tests_only", "test_weakening", "bugfix_small", "bugfix_large", "refactor",
    "feature_small", "feature_large", "performance", "dependency_patch", "dependency_major", "dependency_new",
    "dependency_removed", "config", "ci_cd", "infra_deploy", "security_sensitive", "db_migration", "api_contract",
    "architecture", "agent_self", "governance", "assets", "generated_vendored", "revert", "release", "mass_change",
    "secrets", "suspicious", "unknown",
]


class BugfixAssessment(BaseModel):
    bug: str
    root_cause: str
    root_cause_addressed: bool
    regression_risk: Level
    callers_checked: list[str] = Field(default_factory=list)
    test_coverage: Literal["added", "existing", "none"]
    confidence: Level


class OwnerReviewGuide(BaseModel):
    change_summary: str
    key_risks: list[str] = Field(default_factory=list)
    review_first: list[str] = Field(default_factory=list)
    questions_for_author: list[str] = Field(default_factory=list)
    suggested_tests: list[str] = Field(default_factory=list)


class ModelTriage(BaseModel):
    """Claude's view of the PR. Can only raise the tier above the deterministic floor."""

    category: str
    secondary_categories: list[str] = Field(default_factory=list)
    tier: ModelTier
    confidence: Level
    rationale: str = ""
    intent_matches_diff: bool = True

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        return v if v in CATEGORIES else "unknown"

    @field_validator("secondary_categories")
    @classmethod
    def _known_secondary(cls, v: list[str]) -> list[str]:
        return [c if c in CATEGORIES else "unknown" for c in v]


class Signal(BaseModel):
    name: str
    value: bool | int | str | list[str] = True
    source: Literal["deterministic", "claude", "pr_text", "github", "config"] = "deterministic"
    detail: str = ""


class TriageResult(BaseModel):
    """Optional `triage` block of run_result.json (Milestone 3)."""

    category: str
    secondary_categories: list[str] = Field(default_factory=list)
    tier: Tier
    deterministic_floor: Tier
    claude_tier: ModelTier | None = None
    confidence: Level = "low"
    size: Literal["XS", "S", "M", "L", "XL"] = "XS"
    signals: list[Signal] = Field(default_factory=list)
    decision: Decision = "review_only"
    autonomy: Autonomy = "review"
    blocked_by: list[str] = Field(default_factory=list)
    would_have: list[str] | None = None  # shadow mode: what it would have done
    waiting_for: list[str] = Field(default_factory=list)
    rationale: str = ""
    notes: list[str] = Field(default_factory=list)
    bugfix_assessment: BugfixAssessment | None = None
    owner_review_guide: OwnerReviewGuide | None = None


class AutofixRecord(BaseModel):
    attempt: int
    proposed: bool
    applied: bool = False
    rejected_reason: str | None = None
    commit_sha: str | None = None
    finding_ids: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    reason: str = ""
    risk_notes: str = ""
    patch_preview: str = ""  # bounded unified diff, shown in the comment for suggest_fix


class Action(BaseModel):
    """One write the executor performed (or, in shadow mode, would have)."""

    model_config = ConfigDict(extra="allow")
    type: str


class Stats(BaseModel):
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class ReviewResult(BaseModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    repo: str
    pr_number: int
    head_sha: str
    status: Status
    summary: str
    open_findings: list[Finding] = Field(default_factory=list)
    resolved_findings: list[Finding] = Field(default_factory=list)
    unreviewed_files: list[str] = Field(default_factory=list)
    stop_reason: str | None = None
    stats: Stats = Field(default_factory=Stats)
    # Milestone 2 additions: optional and defaulted, so v1 results and readers keep working.
    ci: CIReport | None = None
    escalation: Escalation | None = None
    context_files: list[ContextFile] = Field(default_factory=list)
    # Milestone 3 additions, also optional.
    triage: TriageResult | None = None
    autofix: AutofixRecord | None = None
    actions: list[Action] = Field(default_factory=list)
    # Claude's raw structured review (triage, bug-fix assessment, guide) for the policy engine; never serialized.
    _model_review: "ModelReview | None" = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _stop_reason_required(self) -> "ReviewResult":
        if self.status != "completed" and not self.stop_reason:
            raise ValueError("stop_reason is required when status is not 'completed'")
        return self


class PreviousReview(BaseModel):
    """State recovered from the bot's earlier PR comment (or a fixture)."""

    comment_id: int | None = None
    head_sha: str
    status: Status
    open_findings: list[Finding] = Field(default_factory=list)
    resolved_findings: list[Finding] = Field(default_factory=list)
    # Milestone 3 state (optional, so older state blocks still parse)
    summary: str = ""
    unreviewed_files: list[str] = Field(default_factory=list)
    stats: Stats | None = None  # the review's own usage, shown again when a later run reuses it
    triage: TriageResult | None = None
    decision: Decision | None = None
    autofix_attempts: int = 0
    failed_fix_ids: list[str] = Field(default_factory=list)  # findings an autofix didn't fix (CI still red)
    human_override: bool = False
    actions: list[Action] = Field(default_factory=list)


# ---------- What Claude must return (controller adds trusted metadata) ----------


class ModelFinding(BaseModel):
    path: str
    line: int | None = None
    kind: str = Field(description="short kebab-case issue type, e.g. null-deref, sql-injection")
    severity: Level
    title: str
    explanation: str
    suggestion: str
    confidence: Level


class ModelReview(BaseModel):
    summary: str
    open_findings: list[ModelFinding] = Field(default_factory=list)
    resolved_finding_ids: list[str] = Field(default_factory=list)
    needs_human: bool = False
    needs_human_reason: str | None = None
    ci_likely_cause: str | None = None
    ci_next_step: str | None = None
    triage: ModelTriage | None = None
    bugfix_assessment: BugfixAssessment | None = None
    owner_review_guide: OwnerReviewGuide | None = None


def _strict(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def triage_json_schemas() -> dict:
    level = {"type": "string", "enum": ["low", "medium", "high"]}
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "triage": _strict({
            "category": {"type": "string", "enum": CATEGORIES},
            "secondary_categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
            "tier": {"type": "string", "enum": ["T0", "T1", "T2", "T3"]},
            "confidence": level,
            "rationale": {"type": "string"},
            "intent_matches_diff": {"type": "boolean"},
        }),
        "bugfix_assessment": _nullable(_strict({
            "bug": {"type": "string"},
            "root_cause": {"type": "string"},
            "root_cause_addressed": {"type": "boolean"},
            "regression_risk": level,
            "callers_checked": strings,
            "test_coverage": {"type": "string", "enum": ["added", "existing", "none"]},
            "confidence": level,
        })),
        "owner_review_guide": _nullable(_strict({
            "change_summary": {"type": "string"},
            "key_risks": strings,
            "review_first": strings,
            "questions_for_author": strings,
            "suggested_tests": strings,
        })),
    }


def model_review_json_schema() -> dict:
    """JSON schema passed to the API's structured-output format (strict-compatible)."""
    level = {"type": "string", "enum": ["low", "medium", "high"]}
    finding = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": ["integer", "null"]},
            "kind": {"type": "string"},
            "severity": level,
            "title": {"type": "string"},
            "explanation": {"type": "string"},
            "suggestion": {"type": "string"},
            "confidence": level,
        },
        "required": ["path", "line", "kind", "severity", "title", "explanation", "suggestion", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "open_findings": {"type": "array", "items": finding},
            "resolved_finding_ids": {"type": "array", "items": {"type": "string"}},
            "needs_human": {"type": "boolean"},
            "needs_human_reason": {"type": ["string", "null"]},
            "ci_likely_cause": {"type": ["string", "null"]},
            "ci_next_step": {"type": ["string", "null"]},
            **triage_json_schemas(),
        },
        "required": ["summary", "open_findings", "resolved_finding_ids", "needs_human", "needs_human_reason",
                     "ci_likely_cause", "ci_next_step", "triage", "bugfix_assessment", "owner_review_guide"],
        "additionalProperties": False,
    }


class FileEdit(BaseModel):
    path: str
    old_text: str
    new_text: str


class NewFile(BaseModel):
    path: str
    content: str


class FixProposal(BaseModel):
    """What the fixer (Claude) proposes. Deterministic Python validates and applies it; Claude never writes."""

    can_fix: bool
    reason: str = ""
    finding_ids: list[str] = Field(default_factory=list)
    edits: list[FileEdit] = Field(default_factory=list)
    new_files: list[NewFile] = Field(default_factory=list)
    commit_message: str = ""
    risk_notes: str = ""


def fix_proposal_json_schema() -> dict:
    return _strict({
        "can_fix": {"type": "boolean"},
        "reason": {"type": "string"},
        "finding_ids": {"type": "array", "items": {"type": "string"}},
        "edits": {"type": "array", "items": _strict({
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}})},
        "new_files": {"type": "array", "items": _strict({"path": {"type": "string"}, "content": {"type": "string"}})},
        "commit_message": {"type": "string"},
        "risk_notes": {"type": "string"},
    })


def finding_id(path: str, kind: str) -> str:
    """Deterministic id from location + issue type. Normalizes the kind to a kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") or "issue"
    return f"{path}:{slug}"
