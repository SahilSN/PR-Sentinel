# PR Sentinel

> An autonomous GitHub assistant that reviews pull requests with Claude, follows up when new commits land, remembers what it found, and stops (or asks for a human) when it can't make progress.

## Current status
_Last updated: 2026-10-24_

**What it does today**
- When run on a PR, it reads the diff and lets Claude investigate with two read-only tools (`get_changed_files`, `read_file`). It then posts **one** review comment and updates that same comment on later pushes, marking earlier findings as resolved or still open.
- Every run is bounded by step, tool-call, token and time limits, and ends as `completed`, `needs_human`, `budget_exceeded` or `error`. `MAX_RUNTIME_SECONDS` is a hard ceiling. Transient API errors (429, 5xx, 529 overloaded, timeouts) are retried up to 3 attempts, but no request or retry gets more time than is left.
- PR content is sent to Claude inside an escaped `<pr_data>` block, so text in a PR can't break out of the untrusted-data section.
- Each run writes `artifacts/run_result.json` (the PROJECT.md §6.4 contract) and appends to `artifacts/actions.jsonl` (a trace for the dashboard).

**Tested vs. not yet tested**
| Area | State |
|---|---|
| Offline fixture runs (`--fake-model`) | ✅ Working; 98 pytest tests pass |
| Real Claude API call | ✅ Live in Actions (Python 3.11) on PRs #3, #4 and #5 |
| Reading a real GitHub PR | ✅ Live (PR data, files, and file contents at the head SHA) |
| Publishing the PR comment | ✅ Live on PRs #4 and #5: one bot comment per PR, updated in place on each push |
| **End-to-end demo (PROJECT.md definition of done)** | ✅ **Passed on PR #5.** A bug commit got the comment flagging the `greeting()` crash; the fix commit updated the same comment, marking it resolved. No manual prompting. |
| GitHub Actions trigger | ✅ Runs on every same-repo PR; the `ANTHROPIC_API_KEY` secret is set |

**Next up**
1. Live-verify Milestone 2: open a disposable PR with a failing check and confirm the comment's CI section, then the dashboard in the run artifacts. This costs a few cents of Claude usage and posts one bot comment.
2. Tune review quality. On PR #3, Claude's one finding was a false positive: it flagged `httpx2` as nonexistent.
3. Rehearse the judge demo (PROJECT.md §14) on a fresh PR and record it as a backup in `demo/`.

**Open PRs**
- #5 `test/live-demo-null-deref`: the live demo PR. Its author marked it **do not merge**; it's kept open as the record of the passing demo.

### Milestone 2 (merged to `main`; see `MILESTONE_2.md`)
| Phase | State | Offline-tested | Live-tested |
|---|---|---|---|
| A. CI check visibility + failure summary | ✅ Done | ✅ | ⏳ Not yet. `checks: read` is now on `main`; needs a PR with real checks |
| B. Related-file context (`list_related_files`) | ✅ Done | ✅ | ⏳ Not yet |
| C. Evidence-based escalation | ✅ Done | ✅ | ⏳ Not yet |
| D. Static dashboard (`python -m pr_agent.dashboard`) | ✅ Done | ✅ | ⏳ The workflow now builds it into each run's artifacts; not yet seen live |

- **CI (A):** reads check runs for the pinned head SHA only. Each check is `passed`, `failed`, `pending` or `neutral`, and the overall state is `passed`, `failed`, `pending`, `mixed`, `not_found` or `unavailable`. A 403 or failed request is `unavailable`, never a pass. PR Sentinel's own check and checks for other commits are ignored. Failed checks include up to 20 annotations. Claude can add a *likely cause*, labelled as its interpretation, only when a check failed.
- **Context (B):** `list_related_files(path)` returns a changed file's local imports, its tests and same-directory files at the same commit. `read_file` can then open those, and only those (at most 4 per run). Symlinks and files over 1 MB are never offered. Findings in related files never get a line number.
- **Dashboard (D):** a static, self-contained HTML page (no JavaScript) for one run, built from `run_result.json` and `actions.jsonl`. It shows status, findings, CI checks and likely cause, escalation and next step, files inspected, tool calls, and usage against limits. Everything is HTML-escaped. There's no cost estimate and no history: it's one run's snapshot. `--demo-data` labels fixture runs.
- **Escalation (C):** a `needs_human` result now carries a reason code, what was checked, what's unknown, and a next step. The codes are `INSUFFICIENT_EVIDENCE` (a serious finding on a file never inspected), `CONFLICTING_EVIDENCE` (CI fails in a reviewed file, but the review found nothing to explain it), `TOOL_FAILURES`, `REPEATED_TOOL_REQUEST`, `INVALID_TOOL_REQUESTS`, `REVIEW_SCHEMA_INVALID`, `MODEL_DECLINED`, `MODEL_REQUESTED` and `NOTHING_REVIEWABLE`. Budget stops stay `budget_exceeded`, with `LIMIT_REACHED`. Open findings or failing CI on their own still give `completed`.

## Changelog
Newest first. Every commit adds an entry here.

- **2026-10-24 — Milestone 2 merged to `main`.** Fast-forwarded `main` to `feature/ci-context-escalation` (the two entries below), then deleted the branch. From the next PR on, reviews read CI checks (new `checks: read` permission), can use `list_related_files`, escalate with reason codes, and attach `dashboard.html` to the run artifacts. The workflow's check is now named **PR Sentinel review**. All of this is tested offline (98 tests) but not yet live.
- **2026-10-24 — Milestone 2, phase D: run dashboard (was branch `feature/ci-context-escalation`).** New `src/pr_agent/dashboard.py`: `python -m pr_agent.dashboard --input … --trace … --output … [--demo-data]` renders one run as static HTML from the real artifacts. It escapes every string, links only to `https://github.com/…`, and shows no invented cost or history. The trace's `run_start` event now records the key limits so the page can show usage against them. The workflow builds `artifacts/dashboard.html` after each review (non-blocking). 12 new tests (98 total), including a fix for trace lines that are valid JSON but not events.
- **2026-10-24 — Milestone 2, phases A–C (was branch `feature/ci-context-escalation`).** New `ci.py` (check runs → `CIReport`), GitHub client `get_check_runs` / `get_check_annotations` / `get_tree` (full-SHA only), and tools `get_ci_status` and `list_related_files`. Adds the escalation reason codes, and a CI section plus a "Human review needed" section in the same comment. `run_result.json` gains **optional** `ci`, `escalation` and `context_files`; v1 results still validate. CLI adds `--no-ci`, and fixtures can carry `ci` and extra `file_contents`. Workflow: `checks: read`, and the job is now named `PR Sentinel review` so the bot can skip its own check. Fixed one controller bug: a rejected tool call no longer counts as "already seen", so a path can be read once `list_related_files` unlocks it. 4 new demo fixtures, 40 new tests (86 total). Real Claude and GitHub not exercised; the SDK request was checked against a mocked transport.
- **2026-10-24 — Live demo passed; QA report merged (#4).** (1) PR #5 (`test/live-demo-null-deref`, not merged by design) ran the full flow live: the bot flagged a None-dereference in `demo/user_service.py`, then updated the same comment as resolved after the fix commit. This confirms the `pull-requests: write` fix. (2) Merged #4: SahilSN's QA report `docs/qa/2026-10-24-runtime-hard-ceiling.md`, rebased to docs only. It records the 5s timeout-floor bug and its verified fix in 3609403. No code changes; 46 tests.
- **2026-10-24 — Runtime boundary fix, PR comment permission, first live run.** (1) Removed the 5s minimum request timeout that let a call started near the deadline overrun it (QA report on PR #4). Requests now get at most the time left, and none starts once the budget is used up. (2) A timeout with under 10s left is now labelled `budget_exceeded`, not `error`, since the client won't retry then. (3) Workflow: `pull-requests: write`, because posting the comment returned 403 on PR #3. (4) Added a comment explaining that `httpx2` is the SDK's real HTTP library, after the live review flagged it as a false positive. 46 tests.
- **2026-10-24 — Retries restored (#3, from the `fix/claude-retries` branch).** The previous commit's retry rule allowed 0 retries at the default 120s budget, so one 529/429 failed the whole review. Retries now run in `ClaudeModel.create` using the SDK's rule (connection errors, timeouts, 408/409/429/5xx, `x-should-retry`), up to 3 attempts with 1s/2s backoff, only while the remaining time covers them. `request_limits()` was removed.
- **2026-10-24 — Hard wall-clock ceiling (follow-up to #2).** The SDK retries timed-out requests, so one Claude call could still take up to 3× the remaining time. Now a call only gets the retries that fit in the time left. A run is labelled `budget_exceeded` only when a request actually timed out; other failures after the deadline stay `error`. Adds `request_limits()` in `claude_client.py` and 2 tests.
- **2026-10-24 — Review hardening (#2, from the `fix/review-hardening` branch).** Three fixes, each with a regression test that fails on the old code: (1) PR text containing `</pr_data>` could close the untrusted-data block, and `<`/`>` are now escaped; (2) each Claude request is capped at the time left in `MAX_RUNTIME_SECONDS`; (3) `times_seen` no longer increases when the same commit is re-reviewed. `compare_with_previous()` gained an optional `head_sha` argument, so existing callers still work.
- **2026-10-24 — Status + changelog in README.** Added this section and the "Current status" section. From now on, every commit updates both, so teammates can see what changed without reading diffs.
- **2026-10-24 — PROJECT.md added; branch rule changed.** The spec is now in the repo. The lead implementer (Claude Max) pushes tested work directly to `main`. Debugging (`fix/*`) and testing (`test/*`) work goes through PRs that Claude Max validates and merges. `AGENTS.md` was updated to match.
- **2026-10-24 — PR Sentinel prototype (#1).** First working slice: contracts, the Claude tool-use loop with limits, the GitHub wrapper, the single upserted comment with stored state, re-review across pushes, the Actions workflow, fixtures and 37 tests.

## Problem
Developers keep re-checking PRs by hand and repeating the same review comments after every push. Most AI review demos produce one block of text and stop.

## Solution
A GitHub Action fires on every PR `opened` / `reopened` / `synchronize` event, so nobody has to prompt it. The agent:

1. Fetches the diff and calls read-only tools (`get_changed_files`, `read_file` at the pinned head SHA) to investigate.
2. Returns a validated, structured review grounded in real files and lines.
3. Keeps **one** PR comment and updates it on each push, marking earlier findings as resolved or still open.
4. Runs under hard limits (steps, tool calls, tokens, wall-clock). It ends as `completed`, `needs_human`, `budget_exceeded` or `error`, never in an endless loop.

Full spec: `PROJECT.md`.

## Tech stack
- Python 3.11+, official `anthropic` SDK (Messages API, client tool use, structured output)
- `httpx` for the GitHub REST API, `pydantic` for contracts, `pytest`
- GitHub Actions (`pull_request` trigger, same-repo branches only)

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
pytest -q
```

### 1. Offline demo (no credentials, no network)
```bash
python -m pr_agent.cli --fixture tests/fixtures/buggy_pr.json --fake-model --dry-run   # finds a None dereference
python -m pr_agent.cli --fixture tests/fixtures/fixed_pr.json --fake-model --dry-run   # same finding marked resolved
python -m pr_agent.cli --fixture tests/fixtures/clean_pr.json --fake-model --dry-run   # no false positives
python -m pr_agent.cli --fixture tests/fixtures/buggy_pr.json --fake-model --max-steps 1  # forced budget stop
```
`--fake-model` is a deterministic regex-based stand-in for Claude, so the whole pipeline runs offline. Drop the flag and set `ANTHROPIC_API_KEY` to send the same fixture to real Claude.

### 2. Real PR, dry run (reads GitHub + calls Claude, posts nothing)
```bash
export GITHUB_TOKEN=...          # fine-grained token: Pull requests read, Contents read, Issues read/write
export ANTHROPIC_API_KEY=...     # API key with API billing; a Claude subscription is not API access
python -m pr_agent.cli --repo OWNER/REPO --pr 12 --dry-run
```

### 3. Real PR, publish (create/update the single bot comment)
```bash
python -m pr_agent.cli --repo OWNER/REPO --pr 12 --publish
```

Each run writes `artifacts/run_result.json` (output contract) and appends to `artifacts/actions.jsonl` (trace).

### 4. Milestone 2 offline demos (no credentials)
```bash
python -m pr_agent.cli --fixture tests/fixtures/ci_failure_pr.json --fake-model --dry-run   # failed check linked to the bug
python -m pr_agent.cli --fixture tests/fixtures/ci_pending_pr.json --fake-model --dry-run   # running check shown as pending
python -m pr_agent.cli --fixture tests/fixtures/cross_file_pr.json --fake-model --dry-run   # bug found via a related helper file
python -m pr_agent.cli --fixture tests/fixtures/ci_conflict_pr.json --fake-model --dry-run  # unexplained CI failure -> needs_human
python -m pr_agent.dashboard --input artifacts/run_result.json --trace artifacts/actions.jsonl \
  --output artifacts/dashboard.html --demo-data                                             # then open artifacts/dashboard.html
```

### Exit codes
`0` = review finished (including deliberate `needs_human` / `budget_exceeded` stops) or skipped because this commit was already reviewed · `1` = `error` status or GitHub failure · `2` = bad flags, missing credentials, or bad input.

## Live demo setup (GitHub Actions)
1. Merge `.github/workflows/pr-agent.yml` into the **default branch**. Actions always runs the workflow version from there.
2. Repo **Settings → Secrets and variables → Actions**:
   - Secret `ANTHROPIC_API_KEY`
   - Variable `ANTHROPIC_MODEL` (optional, defaults to `claude-sonnet-5-5`)
3. Repo **Settings → Actions → General → Workflow permissions**: the workflow requests `issues: write` itself. If your org restricts tokens to read-only, allow it.
4. Open a PR from a branch **in the same repo** with a bug, for example the `find_user` example in `tests/fixtures/buggy_pr.json`. Watch the Actions tab and then the PR comment.
5. Push a fix. A second run updates the same comment and lists the finding under **Resolved since last review**.

Fork PRs are skipped on purpose: they get no secrets, and we never use `pull_request_target` to run untrusted code.

## Configuration
All settings are environment variables; see `.env.example`. Limits (defaults): `MAX_AGENT_STEPS=6`, `MAX_TOOL_CALLS=10`, `MAX_LLM_CALLS=7`, `MAX_TOTAL_TOKENS=60000`, `MAX_RUNTIME_SECONDS=120`, `MAX_FILES=15`, `MAX_FILE_CHARS=16000`, `MAX_DIFF_CHARS=30000`, `MAX_IDENTICAL_TOOL_REQUESTS=1`.

Milestone 2 limits: `MAX_CHECKS=30`, `MAX_ANNOTATED_CHECKS=5`, `MAX_ANNOTATIONS=20`, `MAX_CHECK_SUMMARY_CHARS=2000`, `MAX_RELATED_FILES=8`, `MAX_CONTEXT_READS=4`, `MAX_TOOL_FETCH_FAILURES=2`. New tools count against `MAX_TOOL_CALLS`. `PR_SENTINEL_SELF_CHECK` (default `PR Sentinel review`) must match the workflow job name. The workflow needs `checks: read` to see CI; without it, CI shows as *unavailable*.

`ANTHROPIC_FALLBACKS=on` (the default) enables the API's server-side refusal fallback on supported models. If a safety classifier declines the request, the API retries it on a fallback model instead of failing. Set it to `off` to disable.

## Code map and contracts for teammates

| Module | Responsibility |
|---|---|
| `src/pr_agent/schemas.py` | **Shared contracts**: `PullRequestInfo`, `ChangedFile`, `PreviousReview`, `ReviewResult` (= `run_result.json`). Coordinate before changing these. |
| `src/pr_agent/github_client.py` | GitHub REST wrapper: `get_pull_request`, `get_changed_files`, `read_file(repo, path, ref)`, comments. Pagination, retries, no tokens in errors. |
| `src/pr_agent/agent.py` | Controller: tool-use loop, limits, stop reasons, repair-once validation, trace. Entry: `run_review(pr, files, previous, model=..., read_source=..., limits=...)`. |
| `src/pr_agent/tool_registry.py` | The allowlisted read-only tools (`get_changed_files`, `read_file`, `list_related_files`, `get_ci_status`), path validation, file selection and caps. |
| `src/pr_agent/dashboard.py` | Static HTML dashboard for one run's `run_result.json` + `actions.jsonl` (escaped, no JS). |
| `src/pr_agent/ci.py` | CI evidence: check runs for the head SHA → `CIReport` (states, self-check and other-commit filtering, truncation). |
| `src/pr_agent/claude_client.py` | `ClaudeModel` (real API), `FakeModel` (offline heuristics), `ScriptedModel` (tests). |
| `src/pr_agent/review.py` | Grounds findings against the diff, compares with the previous review, renders the comment with escaping. |
| `src/pr_agent/state.py` | Finds our comment (marker **and** bot author), base64 state block, create-or-update. |
| `src/pr_agent/cli.py` | CLI flags, credential checks, idempotency skip, dry-run vs publish. |

The `run_result.json` format is exactly PROJECT.md §6.4, plus one additive field on each finding: `times_seen` (how many reviewed commits have reported it).

## Known limitations
- The review is a single PR conversation comment, not line-level review comments.
- Same-repo PRs only, and check-run / CI-log investigation (P2) is not built.
- No dollar cost shown, only measured token usage.
- `--fake-model` is a few regex rules for demos and tests, not a real reviewer.
- CI is read once, when the review runs. Checks still running show as *pending*, and the comment isn't updated when they finish (it refreshes on the next push). There's no completed-check trigger.
- CI evidence is check summaries and annotations only; raw job logs aren't read.
- Related-file discovery resolves Python imports. For other languages it only offers same-directory files.
- Request timeouts are httpx per-phase timeouts (connect/read/write), not a total for the whole response. A server trickling bytes could in theory stretch a call past the runtime ceiling. That's unlikely with the Anthropic API, and the Actions `timeout-minutes: 5` is the outer backstop.

## Team
- Lead implementation (Claude Code)
- Debugging on `fix/*` branches (Claude Code)
- Testing, QA, differentiation (Codex)

Shared agent rules live in `AGENTS.md` / `CLAUDE.md`; personal role instructions go in a gitignored `CLAUDE.local.md`. Debugging work goes on `fix/*` branches and testing work on `test/*` branches, each opened as a PR into `main`.

## Demo
See PROJECT.md §14 for the judge script. Keep a recorded successful run in `demo/` as a backup.
