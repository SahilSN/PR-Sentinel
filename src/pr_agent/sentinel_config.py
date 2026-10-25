"""Milestone 3 policy config: `.github/pr-sentinel.yml`, read from the DEFAULT branch only (never from the PR).

A missing or invalid config falls back to `review` autonomy with a visible warning, never to more autonomy.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schemas import Autonomy

CONFIG_PATH = ".github/pr-sentinel.yml"
AUTONOMY_ORDER = ["off", "review", "shadow", "fix", "merge"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MergeConfig(_Strict):
    method: Literal["squash", "merge", "rebase"] = "squash"
    allowed_tiers: list[Literal["T0", "T1"]] = Field(default_factory=lambda: ["T0", "T1"])
    required_checks: list[str] = Field(default_factory=lambda: ["tests"])
    allow_docs_without_ci: bool = True
    max_auto_merges_per_day: int = Field(10, ge=0)
    delete_branch: bool = False
    auto_merge_reverts: bool = False
    owner_can_merge_protected: bool = False


class AutofixConfig(_Strict):
    allowed_tiers: list[Literal["T0", "T1"]] = Field(default_factory=lambda: ["T0", "T1"])
    max_attempts_per_pr: int = Field(2, ge=0)
    max_files: int = Field(3, ge=1)
    max_changed_lines: int = Field(40, ge=1)
    allow_new_test_files: bool = True
    test_paths: list[str] = Field(default_factory=lambda: ["tests/**", "**/test_*.py"])


class Thresholds(_Strict):
    small_max_lines: int = Field(80, ge=1)
    small_max_code_files: int = Field(3, ge=1)
    large_min_lines: int = Field(400, ge=1)
    large_min_files: int = Field(15, ge=1)


DEFAULT_PROTECTED = [".github/**", "src/pr_agent/**", "pyproject.toml", "requirements*.txt", "AGENTS.md",
                     "CLAUDE.md", "PROJECT.md", "MILESTONE_*.md", "LICENSE*", "**/migrations/**", "Dockerfile*"]


class SentinelConfig(_Strict):
    autonomy: Autonomy = "shadow"
    owners: list[str] = Field(default_factory=lambda: ["SahilSN"])
    trusted_bots: list[str] = Field(default_factory=lambda: ["dependabot[bot]"])
    require_owner_for_agent_prs: bool = True
    ci_workflow: str = "ci.yml"  # dispatched after an autofix push (GITHUB_TOKEN pushes don't trigger workflows)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    autofix: AutofixConfig = Field(default_factory=AutofixConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    protected_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED))
    hold_markers: list[str] = Field(default_factory=lambda: ["WIP", "DNM", "do not merge", "[DEMO"])
    docs_link_allowlist: list[str] = Field(
        default_factory=lambda: ["github.com", "docs.github.com", "platform.claude.com", "docs.python.org"])
    categories: dict[str, Literal["review_only", "suggest_fix", "fix_then_merge", "auto_merge", "escalate_owner"]] = \
        Field(default_factory=dict)


def review_only_config(warning: str) -> tuple[SentinelConfig, str]:
    return SentinelConfig(autonomy="review"), warning


def parse_config(text: str | None) -> tuple[SentinelConfig, str | None]:
    """(config, warning). Never raises: anything wrong yields review-only autonomy plus a warning."""
    if text is None:
        return review_only_config(f"No {CONFIG_PATH} on the default branch; running in review-only mode.")
    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
        return SentinelConfig.model_validate(data), None
    except (yaml.YAMLError, ValueError, ValidationError) as exc:
        first = str(exc).splitlines()[0][:200]
        return review_only_config(f"{CONFIG_PATH} is invalid ({first}); running in review-only mode.")


def effective_autonomy(config_level: str, kill_switch: str | None, cli_level: str | None) -> str:
    """Lowest of: repo variable SENTINEL_AUTONOMY (kill switch), the config, and the CLI flag.
    An unrecognised kill-switch value counts as `review`, so a typo can only reduce autonomy."""
    levels = [config_level]
    if kill_switch:
        levels.append(kill_switch.strip().lower() if kill_switch.strip().lower() in AUTONOMY_ORDER else "review")
    if cli_level:
        levels.append(cli_level)
    return min(levels, key=AUTONOMY_ORDER.index)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    """`**` matches any number of directories (including none), `*` stays within one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)
