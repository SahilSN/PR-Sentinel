"""Milestone 3 triage: deterministic signals from the actual diff, plus Claude's view, into one tier and category.

The deterministic floor comes only from the diff and the repository. PR titles, bodies, labels and Claude's
opinion can only RAISE the tier above it, never lower it.
"""

from __future__ import annotations

import ast
import posixpath
import re
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .review import added_lines, removed_lines
from .schemas import BugfixAssessment, ChangedFile, ModelTriage, Signal, TriageResult
from .sentinel_config import CONFIG_PATH, SentinelConfig, matches

TIERS = ["T0", "T1", "T2", "T3", "BLOCK"]

CATEGORY_TIER = {
    "docs": "T0", "comments_only": "T0", "formatting": "T0", "tests_only": "T1", "test_weakening": "T3",
    "bugfix_small": "T1", "bugfix_large": "T2", "refactor": "T2", "feature_small": "T2", "feature_large": "T3",
    "performance": "T2", "dependency_patch": "T1", "dependency_major": "T2", "dependency_new": "T3",
    "dependency_removed": "T2", "config": "T2", "ci_cd": "T3", "infra_deploy": "T3", "security_sensitive": "T3",
    "db_migration": "T3", "api_contract": "T3", "architecture": "T3", "agent_self": "T3", "governance": "T3",
    "assets": "T2", "generated_vendored": "T2", "revert": "T2", "release": "T2", "mass_change": "T3",
    "secrets": "BLOCK", "suspicious": "BLOCK", "unknown": "T2",
}

DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
CODE_EXT = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".rs", ".c", ".cc", ".cpp", ".h",
            ".hpp", ".cs", ".php", ".sh", ".kt", ".swift", ".scala", ".sql"}
CONFIG_EXT = {".yml", ".yaml", ".json", ".toml", ".ini", ".cfg"}
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf",
             ".mp4", ".mov", ".bin", ".exe", ".dll", ".so"}
MANIFESTS = ["requirements*.txt", "**/requirements*.txt", "pyproject.toml", "**/pyproject.toml", "setup.py",
             "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock", "package.json", "**/package.json",
             "package-lock.json", "yarn.lock", "pnpm-lock.yaml"]
SECURITY_WORDS = ("auth", "login", "password", "token", "crypto", "permission", "session", "sanitiz", "sanitis",
                  "secret", "credential")
SECURITY_IDENT = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w*(?:" + "|".join(SECURITY_WORDS) + r")\w*", re.I)

SECRET_PATTERNS = [
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[bp]-[A-Za-z0-9\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic_secret", re.compile(
        r"(?i)\b(?:api[_-]?key|password|passwd|secret|token)\b\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']")),
]
HIDDEN_CHARS = re.compile("[‪-‮⁦-⁩​-‍﻿]")
ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/=]{200,}")
WEAKEN_ADDED = re.compile(r"pytest\.mark\.(?:skip|xfail)|pytest\.(?:skip|xfail)\(|unittest\.skip|@skip\b|\.skipTest\(")
ASSERTISH = re.compile(r"^\s*(?:assert\b|self\.assert\w*\(|pytest\.raises\(|def test_\w*)")
DOC_CLAIM = re.compile(r"^\s*(?:docs?|typo|readme|format(?:ting)?|comments?|style)\b|\b(?:typo|docs only|"
                       r"documentation only|whitespace|formatting only)\b", re.I)
REVERT_TEXT = re.compile(r"^\s*Revert\b|This reverts commit [0-9a-f]{7,40}", re.I | re.M)
REL_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def tier_max(*tiers: str | None) -> str:
    return max((t for t in tiers if t), key=TIERS.index, default="T0")


def raise_tier(tier: str, steps: int = 1) -> str:
    if tier == "BLOCK":
        return tier
    return TIERS[min(TIERS.index(tier) + steps, TIERS.index("T3"))]


def size_bucket(changed_lines: int) -> str:
    for bound, name in ((10, "XS"), (50, "S"), (200, "M"), (500, "L")):
        if changed_lines <= bound:
            return name
    return "XL"


# ---------------- per-file kinds ----------------


def file_kinds(path: str, config: SentinelConfig) -> set[str]:
    base = posixpath.basename(path)
    ext = posixpath.splitext(base)[1].lower()
    lower = path.lower()
    kinds: set[str] = set()
    if matches(path, config.protected_paths):
        kinds.add("protected")
    if path.startswith("src/pr_agent/") or path in (CONFIG_PATH, ".github/workflows/pr-agent.yml"):
        kinds.add("agent_self")
    if path.startswith((".github/workflows/", ".github/actions/")):
        kinds.add("ci_cd")
    if matches(path, ["LICENSE*", "CODEOWNERS", ".github/CODEOWNERS", "SECURITY.md", "AGENTS.md", "CLAUDE.md",
                      "PROJECT.md"]):
        kinds.add("governance")
    if matches(path, ["Dockerfile*", "**/Dockerfile*", "docker-compose*", "**/*.tf", "k8s/**", "deploy/**",
                      "helm/**", "infra/**"]):
        kinds.add("infra")
    if matches(path, ["**/migrations/**", "**/*.sql"]):
        kinds.add("migration")
    if matches(path, MANIFESTS):
        kinds.add("manifest")
    if matches(path, ["vendor/**", "third_party/**", "**/*_pb2.py", "**/*.min.js"]):
        kinds.add("generated")
    if matches(path, ["**/schemas.py", "**/*.proto", "**/openapi*.y*ml", "**/openapi*.json"]):
        kinds.add("api_contract")
    is_test = matches(path, config.autofix.test_paths + ["tests/**", "**/test_*.py", "**/*_test.py",
                                                         "**/*.test.js", "**/*.test.ts"])
    if is_test:
        kinds.add("test")
    if (ext in DOC_EXT or path.startswith("docs/") or base.upper().startswith(("README", "CHANGELOG"))) \
            and not is_test and ext not in CODE_EXT:
        kinds.add("doc")
    if ext in ASSET_EXT:
        kinds.add("asset")
    if ext in CODE_EXT:
        kinds.add("code")
    if ext in CONFIG_EXT and not kinds & {"ci_cd", "infra", "manifest", "api_contract"}:
        kinds.add("config")
    if any(w in seg for seg in lower.split("/") for w in SECURITY_WORDS) or base.startswith(".env"):
        kinds.add("security")
    return kinds


# ---------------- AST equivalence (parse only, never execute) ----------------


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def python_equivalence(base: str | None, head: str | None) -> str:
    """'identical' (same AST), 'docstrings_only', 'different', or 'unknown' (couldn't fetch or parse)."""
    if base is None or head is None:
        return "unknown"
    try:
        a, b = ast.parse(base), ast.parse(head)
    except (SyntaxError, ValueError):
        return "unknown"
    if ast.dump(a) == ast.dump(b):
        return "identical"
    if ast.dump(_strip_docstrings(a)) == ast.dump(_strip_docstrings(b)):
        return "docstrings_only"
    return "different"


def whitespace_only(patch: str | None) -> bool:
    """Non-Python fallback: the diff only changes whitespace."""
    def norm(lines):
        return Counter("".join(l.split()) for l in lines if l.strip())
    return bool(patch) and norm(removed_lines(patch)) == norm(t for _, t in added_lines(patch))


# ---------------- dependencies ----------------


REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)(?:\[[^\]]*\])?\s*(.*?)\s*(?:#.*)?$")
VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _requirements(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith(("#", "-")):
            continue
        m = REQ_LINE.match(line.split(";")[0])
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def parse_dependencies(path: str, text: str | None) -> dict[str, str] | None:
    if text is None:
        return None
    base = posixpath.basename(path)
    try:
        if base.startswith("requirements") and base.endswith(".txt"):
            return _requirements(text)
        if base == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project", {})
            reqs = list(project.get("dependencies", []))
            for extra in project.get("optional-dependencies", {}).values():
                reqs += list(extra)
            return _requirements("\n".join(reqs))
        if base == "package.json":
            import json
            data = json.loads(text)
            return {k.lower(): str(v) for section in ("dependencies", "devDependencies")
                    for k, v in (data.get(section) or {}).items()}
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def dependency_changes(base: dict[str, str], head: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"added": [], "removed": [], "major": [], "minor_or_patch": []}
    out["added"] = sorted(set(head) - set(base))
    out["removed"] = sorted(set(base) - set(head))
    for name in sorted(set(base) & set(head)):
        if base[name] == head[name]:
            continue
        old, new = VERSION.search(base[name] or ""), VERSION.search(head[name] or "")
        if old and new and old.group(1) == new.group(1):
            out["minor_or_patch"].append(name)
        else:
            out["major"].append(name)  # unknown or changed major version: treat as major
    return out


# ---------------- the deterministic pass ----------------


@dataclass
class DeterministicTriage:
    categories: dict[str, str] = field(default_factory=dict)  # category -> tier
    signals: list[Signal] = field(default_factory=list)
    floor: str = "T0"
    size: str = "XS"
    changed_lines: int = 0
    code_files: list[str] = field(default_factory=list)
    code_change: bool = False
    protected: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    broken_links: list[str] = field(default_factory=list)

    def add(self, category: str, tier: str | None = None) -> None:
        self.categories[category] = tier_max(self.categories.get(category), tier or CATEGORY_TIER[category])

    def signal(self, name: str, value=True, detail: str = "", source: str = "deterministic", floor: str | None = None):
        self.signals.append(Signal(name=name, value=value, detail=detail[:300], source=source))
        if floor:
            self.floor = tier_max(self.floor, floor)


def deterministic_triage(
    files: list[ChangedFile],
    unreviewed: list[str],
    config: SentinelConfig,
    *,
    title: str = "",
    body: str = "",
    base_text: Callable[[str], str | None] = lambda p: None,
    head_text: Callable[[str], str | None] = lambda p: None,
    tree_paths: set[str] | None = None,
) -> DeterministicTriage:
    t = DeterministicTriage()
    th = config.thresholds
    t.changed_lines = sum(f.additions + f.deletions for f in files)
    t.size = size_bucket(t.changed_lines)
    t.signal("size", t.size, f"{t.changed_lines} changed lines in {len(files)} files")
    kinds = {f.path: file_kinds(f.path, config) for f in files}

    # Hard rules first: BLOCK signals scan every added line.
    for f in files:
        for line_no, text in added_lines(f.patch):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    t.block_reasons.append(f"possible secret ({name}) at {f.path}:{line_no}")
                    t.add("secrets")
                    t.signal("secret", f"{f.path}:{line_no}", f"{name} pattern (value not shown)", floor="BLOCK")
                    break
            if HIDDEN_CHARS.search(text):
                t.block_reasons.append(f"hidden/bidirectional Unicode at {f.path}:{line_no}")
                t.add("suspicious")
                t.signal("hidden_unicode", f"{f.path}:{line_no}", "bidi or zero-width character", floor="BLOCK")
            if ENCODED_BLOB.search(text) and re.search(r"\b(?:exec|eval|b64decode|decompress|marshal)\b", text):
                t.block_reasons.append(f"encoded blob fed to exec/eval at {f.path}:{line_no}")
                t.add("suspicious")
                t.signal("encoded_exec", f"{f.path}:{line_no}", floor="BLOCK")
            if "code" in kinds[f.path] and len(text) > 1000:
                t.block_reasons.append(f"minified/obfuscated-looking line at {f.path}:{line_no}")
                t.add("suspicious")
                t.signal("minified_code", f"{f.path}:{line_no}", floor="BLOCK")

    # Path-based categories.
    for f in files:
        k = kinds[f.path]
        if "protected" in k:
            t.protected.append(f.path)
        for kind, category in (("agent_self", "agent_self"), ("ci_cd", "ci_cd"), ("governance", "governance"),
                               ("infra", "infra_deploy"), ("migration", "db_migration"), ("asset", "assets"),
                               ("generated", "generated_vendored"), ("api_contract", "api_contract"),
                               ("config", "config")):
            if kind in k:
                t.add(category)
        if "security" in k:
            t.add("security_sensitive")
            t.signal("security_path", f.path)
        if "code" in k and any(SECURITY_IDENT.match(text) for _, text in added_lines(f.patch)):
            t.add("security_sensitive")
            t.signal("security_identifier", f.path, "security-related function or class added")
    if t.protected:
        t.signal("protected_paths", sorted(t.protected), floor="T3")

    # Unreviewed files can never be auto-merged.
    if unreviewed:
        t.signal("unreviewed_files", sorted(unreviewed), "no diff, binary or over limits", floor="T2")

    # Code vs. docs/comments/formatting.
    for f in files:
        k = kinds[f.path]
        if "code" not in k:
            continue
        if f.path.endswith(".py") and f.status in ("modified", "renamed", "changed"):
            eq = python_equivalence(base_text(f.path), head_text(f.path))
            t.signal("ast_equivalence", eq, f.path)
        elif f.status in ("modified", "changed"):
            eq = "identical" if whitespace_only(f.patch) else "different"
        else:
            eq = "different"  # added/removed code is always a change
        if eq not in ("identical", "docstrings_only"):
            if "test" not in k:
                t.code_files.append(f.path)
            t.code_change = True
        elif eq == "docstrings_only" or any(text.strip().startswith("#") for _, text in added_lines(f.patch)):
            t.add("comments_only")
        else:
            t.add("formatting")
    if t.code_change:
        t.signal("code_change", True, ", ".join(t.code_files)[:200], floor="T1")

    if files and all("doc" in kinds[f.path] for f in files) and not t.protected:
        t.add("docs")

    # Tests: additions only -> tests_only; weakening -> T3.
    weakened = []
    for f in files:
        if "test" not in kinds[f.path]:
            continue
        if f.status == "removed":
            weakened.append(f"{f.path} deleted")
            continue
        removed_asserts = sum(1 for line in removed_lines(f.patch) if ASSERTISH.match(line))
        added_asserts = sum(1 for _, line in added_lines(f.patch) if ASSERTISH.match(line))
        if removed_asserts > added_asserts:
            weakened.append(f"{f.path}: {removed_asserts - added_asserts} assertion(s)/test(s) removed")
        for line_no, text in added_lines(f.patch):
            if WEAKEN_ADDED.search(text) or (re.search(r"#\s*noqa", text) and "assert" in text):
                weakened.append(f"{f.path}:{line_no} adds a skip/xfail/noqa")
    if weakened:
        t.add("test_weakening")
        t.signal("test_weakening", weakened[:10], floor="T3")
    test_files = [f for f in files if "test" in kinds[f.path]]
    if test_files and len(test_files) == len(files) and not weakened and all(f.deletions == 0 for f in files):
        t.add("tests_only")

    # Size thresholds.
    if t.changed_lines > th.large_min_lines or len(files) > th.large_min_files:
        t.add("mass_change")
        t.signal("mass_change", True, f"{t.changed_lines} lines / {len(files)} files", floor="T3")
    small = len(t.code_files) <= th.small_max_code_files and t.changed_lines <= th.small_max_lines
    if t.code_change:
        t.signal("code_size", "small" if small else "large",
                 f"{len(t.code_files)} code files, {t.changed_lines} lines (small: <= {th.small_max_code_files} "
                 f"files, <= {th.small_max_lines} lines)")

    # Architecture: new packages or code spread across many top-level directories.
    new_packages = [f.path for f in files if f.status == "added" and f.path.endswith("__init__.py")]
    top_dirs = {f.path.split("/")[0] for f in files if "code" in kinds[f.path] and "/" in f.path}
    if new_packages or len(top_dirs) >= 5:
        t.add("architecture")
        t.signal("architecture", new_packages or sorted(top_dirs), floor="T3")

    # Dependencies (parsed from the manifests at base and head).
    for f in files:
        if "manifest" not in kinds[f.path]:
            continue
        old, new = parse_dependencies(f.path, base_text(f.path)), parse_dependencies(f.path, head_text(f.path))
        if old is None or new is None:
            t.signal("manifest_unparsed", f.path, floor="T2")
            continue
        changes = dependency_changes(old, new)
        for key, category in (("added", "dependency_new"), ("removed", "dependency_removed"),
                              ("major", "dependency_major"), ("minor_or_patch", "dependency_patch")):
            if changes[key]:
                t.add(category)
                t.signal(category, changes[key])

    # Release: a version line changed.
    for f in files:
        if any(re.match(r"\s*(?:version\s*=|__version__\s*=)", text) for _, text in added_lines(f.patch)):
            t.add("release")
            t.signal("version_bump", f.path)

    # Docs links: broken relative links (fixable) and unknown external domains.
    for f in files:
        if "doc" not in kinds[f.path]:
            continue
        for line_no, text in added_lines(f.patch):
            for target in REL_LINK.findall(text):
                if target.startswith(("http://", "https://")):
                    domain = re.sub(r"^https?://([^/:]+).*$", r"\1", target).lower()
                    if not any(domain == d or domain.endswith("." + d) for d in config.docs_link_allowlist):
                        t.signal("external_link", domain, f"{f.path}:{line_no}", floor="T1")
                elif not target.startswith(("mailto:", "/")) and tree_paths is not None:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(f.path), target))
                    if resolved not in tree_paths and not any(p.startswith(resolved + "/") for p in tree_paths):
                        t.broken_links.append(f"{f.path}:{line_no} -> {target}")
        if t.broken_links:
            t.signal("broken_relative_links", t.broken_links[:10])

    # PR text is untrusted: it can only raise the tier.
    if REVERT_TEXT.search(f"{title}\n{body}"):
        t.add("revert")
        t.signal("revert_text", True, source="pr_text")
    non_doc = [f.path for f in files if "doc" not in kinds[f.path]]
    if DOC_CLAIM.search(title) and (t.code_change or non_doc):
        t.signal("intent_mismatch", non_doc[:5] or True,
                 f"title {title[:80]!r} claims docs/format, but non-docs files changed", source="pr_text", floor="T2")

    t.floor = tier_max(t.floor, *t.categories.values())
    return t


# ---------------- combining with Claude ----------------


def _normalize(category: str, det: DeterministicTriage) -> str:
    small = not det.code_change or any(s.name == "code_size" and s.value == "small" for s in det.signals)
    if category == "bugfix_small" and not small:
        return "bugfix_large"
    if category == "feature_small" and not small:
        return "feature_large"
    return category


def combine(det: DeterministicTriage, model: ModelTriage | None, config: SentinelConfig) -> TriageResult:
    signals = list(det.signals)
    floor = det.floor
    categories = dict(det.categories)
    claude_tier = None
    confidence = "low"
    rationale = ""
    primary_hint = None
    if model is not None:
        claude_tier = model.tier
        confidence = model.confidence
        rationale = model.rationale
        primary_hint = _normalize(model.category, det)
        for c in [model.category, *model.secondary_categories]:
            c = _normalize(c, det)
            categories[c] = tier_max(categories.get(c), CATEGORY_TIER[c])
        signals.append(Signal(name="claude_triage", value=f"{model.category}/{model.tier}/{model.confidence}",
                              source="claude", detail=model.rationale))
        if not model.intent_matches_diff:
            floor = tier_max(floor, "T2")
            signals.append(Signal(name="intent_mismatch", source="claude", detail="Claude: PR text doesn't match diff"))
    elif det.code_change:
        categories["unknown"] = tier_max(categories.get("unknown"), "T2")  # code with no Claude classification

    claude_effective = claude_tier
    if model is not None and model.confidence == "low":
        claude_effective = raise_tier(claude_tier)
        signals.append(Signal(name="low_confidence", source="claude", detail="tier raised one level"))
    tier = tier_max(floor, claude_effective, *categories.values())

    if not categories:
        categories["unknown"] = "T2"
        tier = tier_max(tier, "T2")
    ranked = sorted(categories.items(), key=lambda kv: (TIERS.index(kv[1]), kv[0] == primary_hint), reverse=True)
    primary = ranked[0][0]
    if primary_hint in categories and categories[primary_hint] == categories[primary]:
        primary = primary_hint
    return TriageResult(
        category=primary,
        secondary_categories=[c for c, _ in ranked if c != primary],
        tier=tier,
        deterministic_floor=floor,
        claude_tier=claude_tier,
        confidence=confidence,
        size=det.size,
        signals=signals,
        rationale=rationale,
    )


def bugfix_gaps(triage: TriageResult, assessment: BugfixAssessment | None) -> list[str]:
    """What's missing from MILESTONE_3.md §4.6 before a bug fix may merge (CI and findings are checked elsewhere)."""
    if not (triage.category.startswith("bugfix") or any(c.startswith("bugfix") for c in triage.secondary_categories)):
        return []
    gaps = []
    if assessment is None:
        return ["no bug-fix assessment"]
    if len(assessment.bug.strip()) < 10:
        gaps.append("bug not identified concretely")
    if not assessment.root_cause_addressed:
        gaps.append("root cause not addressed")
    if assessment.test_coverage == "none":
        gaps.append("no tests cover the fix")
    if assessment.confidence != "high" or triage.confidence != "high":
        gaps.append("confidence below high")
    if assessment.regression_risk == "high":
        gaps.append("high regression risk")
    return gaps
