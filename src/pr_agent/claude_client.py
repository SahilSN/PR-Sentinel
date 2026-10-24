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


MAX_ATTEMPTS = 3  # one try plus two retries, like the SDK default


class ClaudeModel:
    def __init__(self, settings: Settings):
        import anthropic  # imported lazily so offline mode doesn't need the SDK configured

        self.settings = settings
        # Credentials come from the environment (ANTHROPIC_API_KEY); never passed around in code.
        # Retries happen in create(), not in the SDK: the SDK would give a timed-out retry a fresh full timeout.
        self.client = anthropic.Anthropic(timeout=90.0, max_retries=0)
        self._deadline: float | None = None  # set on the first request

    def create(self, system: str, messages: list, tools: list):
        # Every attempt gets at most what's left of MAX_RUNTIME_SECONDS, so a slow or failing API can't run past the
        # agent's wall-clock limit (and the Actions job timeout) before the controller gets to stop the run.
        if self._deadline is None:
            self._deadline = time.monotonic() + self.settings.limits.max_runtime_seconds
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
        for attempt in range(MAX_ATTEMPTS):
            remaining = self._deadline - time.monotonic()
            if remaining <= 1.0:  # never start a request the budget can't cover (no minimum-timeout overrun)
                raise TimeoutError("MAX_RUNTIME_SECONDS was used up before the model request could start")
            client = self.client.with_options(timeout=min(90.0, remaining), max_retries=0)
            try:
                if self.settings.use_fallbacks:
                    # Server-side refusal fallback: if a safety classifier declines, the API retries on a fallback model.
                    return client.beta.messages.create(
                        **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                    )
                return client.messages.create(**kwargs)
            except Exception as exc:
                backoff = 2.0**attempt
                out_of_time = self._deadline - time.monotonic() < backoff + 5.0
                if attempt == MAX_ATTEMPTS - 1 or out_of_time or not is_retryable(exc):
                    raise
                time.sleep(backoff)


def is_retryable(exc: Exception) -> bool:
    """The SDK's own retry rule: connection errors and timeouts, 408, 409, 429 and 5xx (including 529 overloaded)."""
    import anthropic

    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, anthropic.APIStatusError):
        should_retry = exc.response.headers.get("x-should-retry")
        if should_retry in ("true", "false"):
            return should_retry == "true"
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return False


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
    """Deterministic heuristic reviewer so the whole pipeline runs without credentials. One tool call per turn:

    1. read_file the first changed file
    2. get_ci_status, if <pr_data> shows a failed check
    3. list_related_files for that file, if it has imports
    4. read_file each related file it imports (at most 2)
    5. return the ModelReview JSON from a few regex rules (plus None-returning helpers found in step 4)
    """

    def create(self, system: str, messages: list, tools: list):
        data = _extract_pr_data(messages)
        calls, results = _tool_history(messages)
        first = data["files"][0]["path"] if data["files"] else None
        names = {t["name"] for t in tools}
        if first and ("read_file", first) not in calls:
            return tool_response(("read_file", {"path": first}))
        ci = data.get("ci") or {}
        if ci.get("state") in ("failed", "mixed") and "get_ci_status" in names and ("get_ci_status", None) not in calls:
            return tool_response(("get_ci_status", {}))
        first_patch = data["files"][0].get("patch") or "" if first else ""
        has_imports = re.search(r"^[+ ]\s*(from\s+\S+\s+import|import\s+\w)", first_patch, re.M)
        if first and has_imports and "list_related_files" in names and ("list_related_files", first) not in calls:
            return tool_response(("list_related_files", {"path": first}))
        related = results.get(("list_related_files", first))
        if related:
            imported = [r["path"] for r in json.loads(related).get("related_files", [])
                        if r["reason"] == "imported by the changed file"][:2]
            for path in imported:
                if ("read_file", path) not in calls:
                    return tool_response(("read_file", {"path": path}))
        helper_texts = {path: text for (name, path), text in results.items()
                        if name == "read_file" and path != first and not text.startswith("Error")}
        ci_payload = results.get(("get_ci_status", None))
        return text_response(json.dumps(_heuristic_review(data, helper_texts, json.loads(ci_payload) if ci_payload else None)))


def _tool_history(messages: list) -> tuple[set, dict]:
    """(name, path) of every tool call so far, and the successful results keyed the same way."""
    by_id, calls, results = {}, set(), {}
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if getattr(b, "type", None) == "tool_use":
                    key = (b.name, (b.input or {}).get("path"))
                    by_id[b.id] = key
                    calls.add(key)
        elif m["role"] == "user" and isinstance(m["content"], list):
            for r in m["content"]:
                if isinstance(r, dict) and r.get("type") == "tool_result" and not r.get("is_error"):
                    results[by_id.get(r["tool_use_id"])] = r["content"]
    return calls, results


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


def _none_returning_functions(helper_texts: dict[str, str]) -> dict[str, str]:
    """{function name: helper path} for helpers whose source shows they can return None."""
    found: dict[str, str] = {}
    for path, text in helper_texts.items():
        current = None
        for raw in text.splitlines():
            line = re.sub(r"^\s*\d+  ", "", raw)  # strip read_file's line numbers
            m = re.match(r"\s*def\s+(\w+)\s*\(.*?\)\s*(->\s*(.+?))?:", line)
            if m:
                current = m.group(1)
                if m.group(3) and re.search(r"Optional\[|\|\s*None|None\s*\|", m.group(3)):
                    found[current] = path
                continue
            if current and re.match(r"\s*return\s+None\b", line):
                found[current] = path
    return found


def _heuristic_review(data: dict, helper_texts: dict[str, str] | None = None, ci: dict | None = None) -> dict:
    findings = []
    helpers = _none_returning_functions(helper_texts or {})
    for f in data["files"]:
        path, maybe_none = f["path"], {}
        for line_no, text in _added_lines(f.get("patch") or ""):
            m = OPTIONAL_CALL.match(text)
            h = next((re.match(rf"^\s*(\w+)\s*=\s*.*\b{fn}\(", text) for fn in helpers
                      if re.match(rf"^\s*(\w+)\s*=\s*.*\b{fn}\(", text)), None)
            if m or h:
                fn = next((fn for fn in helpers if h and re.search(rf"\b{fn}\(", text)), None)
                maybe_none[(m or h).group(1)] = (line_no, fn)
                continue
            for var in list(maybe_none):
                if re.search(rf"\bif\s+(not\s+)?{var}\s*(:|\band\b|\bor\b)|\b{var}\s+is\s+(not\s+)?None", text):
                    maybe_none.pop(var)
                elif re.search(rf"\b{var}\.\w|\b{var}\[", text):
                    _, fn = maybe_none.pop(var)
                    source = (f"`{var}` comes from `{fn}()`, and reading {helpers[fn]} confirms it can return None"
                              if fn else f"`{var}` comes from a lookup that can return None")
                    findings.append(_finding(
                        path, line_no, "null-deref", "high", f"Possible None dereference of `{var}`",
                        f"{source}, then `{var}` is used without a check.",
                        f"Handle the missing case (e.g. `if {var} is None: ...`) before using `{var}`.",
                    ))
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
    likely_cause = next_step = None
    failed = [c for c in (ci or {}).get("checks", []) if c.get("state") == "failed"]
    if failed:
        notes = [a for c in failed for a in c.get("annotations", [])]
        match = next((a for a in notes for x in findings if a.get("path") == x["path"]), None)
        if match:
            likely_cause = (f"The failing check reports \"{match['message']}\" in {match['path']}, the same file as "
                            "the None-dereference finding above, so that bug is the likely cause.")
            next_step = "Fix the finding above, then push; the check should pass."
        else:
            next_step = f"Open the log of the failing check '{failed[0]['name']}': the cause isn't clear from the evidence."
    return {"summary": summary, "open_findings": findings, "resolved_finding_ids": resolved,
            "needs_human": False, "needs_human_reason": None, "ci_likely_cause": likely_cause,
            "ci_next_step": next_step}


def _finding(path, line, kind, severity, title, explanation, suggestion):
    return {"path": path, "line": line, "kind": kind, "severity": severity, "title": title,
            "explanation": explanation, "suggestion": suggestion, "confidence": "medium"}
