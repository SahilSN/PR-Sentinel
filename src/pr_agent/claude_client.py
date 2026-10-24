"""Model adapters. `ClaudeModel` calls the real API; `FakeModel` is a deterministic offline stand-in.

Both expose: create(system, messages, tools) -> response with .content, .stop_reason, .usage
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .config import Settings
from .schemas import model_review_json_schema

PR_DATA_OPEN, PR_DATA_CLOSE = "<pr_data>", "</pr_data>"


class ClaudeModel:
    def __init__(self, settings: Settings):
        import anthropic  # imported lazily so offline mode doesn't need the SDK configured

        self.settings = settings
        # Credentials come from the environment (ANTHROPIC_API_KEY); never passed around in code.
        self.client = anthropic.Anthropic(timeout=90.0, max_retries=2)
        self._deadline: float | None = None  # set on the first request

    def create(self, system: str, messages: list, tools: list):
        # Cap each request attempt at what's left of MAX_RUNTIME_SECONDS, so one slow call can't run far past the
        # agent's wall-clock limit (and the Actions job timeout) before the controller gets to stop the run.
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + self.settings.limits.max_runtime_seconds
        client = self.client.with_options(timeout=min(90.0, max(self._deadline - now, 5.0)))
        kwargs = dict(
            model=self.settings.model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
            output_config={
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": model_review_json_schema()},
            },
        )
        if self.settings.use_fallbacks:
            # Server-side refusal fallback: if a safety classifier declines, the API retries on a fallback model.
            return client.beta.messages.create(
                **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        return client.messages.create(**kwargs)


# ---------- Offline doubles ----------


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


def text_response(text: str, stop_reason: str = "end_turn") -> FakeResponse:
    return FakeResponse([Block("text", text=text)], stop_reason, Usage(100, len(text) // 4))


def tool_response(*calls: tuple[str, dict]) -> FakeResponse:
    blocks = [Block("tool_use", id=f"toolu_{i}", name=n, input=a) for i, (n, a) in enumerate(calls)]
    return FakeResponse(blocks, "tool_use", Usage(100, 20))


class ScriptedModel:
    """Returns pre-baked responses in order; records every request. Used in unit tests."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[list] = []

    def create(self, system: str, messages: list, tools: list):
        self.requests.append([dict(m) for m in messages])
        if not self.responses:
            raise AssertionError("ScriptedModel ran out of responses")
        return self.responses.pop(0)


OPTIONAL_CALL = re.compile(r"^\s*(\w+)\s*=\s*.*\b(get|find\w*|first|search|match|lookup\w*)\(")
SQL_FORMAT = re.compile(r"\.execute\(\s*(f[\"']|[\"'].*[\"']\s*(%|\+|\.format))")
SECRET = re.compile(r"(api_key|apikey|password|secret|token)\s*=\s*[\"'][^\"']{8,}[\"']", re.I)


class FakeModel:
    """Deterministic heuristic reviewer so the whole pipeline runs without credentials.

    Turn 1: asks to read_file the first changed file (exercises the tool loop).
    Turn 2: scans added lines with a few regex rules and returns the ModelReview JSON.
    """

    def create(self, system: str, messages: list, tools: list):
        data = _extract_pr_data(messages)
        assistant_turns = sum(1 for m in messages if m["role"] == "assistant")
        if assistant_turns == 0 and data["files"]:
            return tool_response(("read_file", {"path": data["files"][0]["path"]}))
        return text_response(json.dumps(_heuristic_review(data)))


def _extract_pr_data(messages: list) -> dict:
    first = messages[0]["content"]
    text = first if isinstance(first, str) else first[0]["text"]
    start = text.index(PR_DATA_OPEN) + len(PR_DATA_OPEN)
    return json.loads(text[start : text.index(PR_DATA_CLOSE)])


def _added_lines(patch: str):
    """Yield (new_line_number, text) for every added line in a unified diff."""
    line_no = None
    for raw in patch.splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if m:
            line_no = int(m.group(1))
            continue
        if line_no is None or raw.startswith("-") or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            yield line_no, raw[1:]
        line_no += 1


def _heuristic_review(data: dict) -> dict:
    findings = []
    for f in data["files"]:
        path, maybe_none = f["path"], {}
        for line_no, text in _added_lines(f.get("patch") or ""):
            m = OPTIONAL_CALL.match(text)
            if m:
                maybe_none[m.group(1)] = line_no
                continue
            for var in list(maybe_none):
                if re.search(rf"\bif\s+(not\s+)?{var}\s*(:|\band\b|\bor\b)|\b{var}\s+is\s+(not\s+)?None", text):
                    maybe_none.pop(var)
                elif re.search(rf"\b{var}\.\w", text):
                    findings.append(_finding(
                        path, line_no, "null-deref", "high", f"Possible None dereference of `{var}`",
                        f"`{var}` comes from a lookup that can return None, then `{var}.` is accessed without a check.",
                        f"Handle the missing case (e.g. `if {var} is None: ...`) before using `{var}`.",
                    ))
                    maybe_none.pop(var)
            if re.search(r"\b(eval|exec)\(", text):
                findings.append(_finding(path, line_no, "code-injection", "high", "Dynamic code execution",
                                         "eval/exec on data can run arbitrary code.", "Parse the input explicitly instead."))
            if SQL_FORMAT.search(text):
                findings.append(_finding(path, line_no, "sql-injection", "high", "SQL built with string formatting",
                                         "Query text is built from variables, allowing SQL injection.",
                                         "Use parameterized queries."))
            if SECRET.search(text):
                findings.append(_finding(path, line_no, "hardcoded-secret", "medium", "Hard-coded credential",
                                         "A secret-looking literal is committed to source.", "Load it from the environment."))
    reported = {f"{x['path']}:{x['kind']}" for x in findings}
    pr_paths = {f["path"] for f in data["files"]}
    resolved = [p["id"] for p in data.get("previous_findings", []) if p["id"] not in reported and p["path"] in pr_paths]
    summary = (
        f"Found {len(findings)} likely issue(s) in the changed code." if findings else "No significant issues found in the changed code."
    )
    if resolved:
        summary += f" {len(resolved)} previously reported issue(s) appear fixed."
    return {"summary": summary, "open_findings": findings, "resolved_finding_ids": resolved,
            "needs_human": False, "needs_human_reason": None}


def _finding(path, line, kind, severity, title, explanation, suggestion):
    return {"path": path, "line": line, "kind": kind, "severity": severity, "title": title,
            "explanation": explanation, "suggestion": suggestion, "confidence": "medium"}
