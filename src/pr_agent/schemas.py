"""Shared data contracts. Teammates build against these - coordinate before changing names or types.

Input:  PullRequestInfo + list[ChangedFile] + PreviousReview | None   (PROJECT.md 6.3)
Output: ReviewResult, written to artifacts/run_result.json            (PROJECT.md 6.4)
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
        },
        "required": ["summary", "open_findings", "resolved_finding_ids", "needs_human", "needs_human_reason",
                     "ci_likely_cause", "ci_next_step"],
        "additionalProperties": False,
    }


def finding_id(path: str, kind: str) -> str:
    """Deterministic id from location + issue type. Normalizes the kind to a kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") or "issue"
    return f"{path}:{slug}"
