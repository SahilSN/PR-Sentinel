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
        },
        "required": ["summary", "open_findings", "resolved_finding_ids", "needs_human", "needs_human_reason"],
        "additionalProperties": False,
    }


def finding_id(path: str, kind: str) -> str:
    """Deterministic id from location + issue type. Normalizes the kind to a kebab-case slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") or "issue"
    return f"{path}:{slug}"
