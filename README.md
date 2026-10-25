# PR Sentinel

> An autonomous GitHub assistant that reviews pull requests with Claude, follows up when new commits land, remembers what it found, and stops (or asks for a human) when it can't make progress. Milestone 3 (on `main`, running at `shadow` autonomy) adds triage: it merges what's provably safe, fixes small bugs, and hands risky changes to the owner with a review guide.

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
| Offline fixture runs (`--fake-model`) | ✅ Working; 239 tests pass (also in GitHub Actions CI on Python 3.11) |
| Real Claude API call | ✅ Live in Actions (Python 3.11) on PRs #3, #4 and #5 |
| Reading a real GitHub PR | ✅ Live (PR data, files, and file contents at the head SHA) |
| Publishing the PR comment | ✅ Live on PRs #4 and #5: one bot comment per PR, updated in place on each push |
| **End-to-end demo (PROJECT.md definition of done)** | ✅ **Passed on PR #5.** A bug commit got the comment flagging the `greeting()` crash; the fix commit updated the same comment, marking it resolved. No manual prompting. |
| GitHub Actions trigger | ✅ Runs on every same-repo PR; the `ANTHROPIC_API_KEY` secret is set |

**Next up**
1. Milestone 3 live checks: confirm the shadow-mode Triage section on the next real PR, then (after the repo settings below) run the `MILESTONE_3.md` §18 demo PRs at `merge` autonomy.
2. See a live escalation (Phase C): e.g. a PR whose CI fails in a changed file for a reason the diff doesn't show.
3. Tune review quality. On PR #3, Claude's one finding was a false positive: it flagged `httpx2` as nonexistent.
4. Rehearse the judge demo (PROJECT.md §14) on a fresh PR and record it as a backup in `demo/`.

**Open PRs**
- #5 `test/live-demo-null-deref` and #6 `test/live-demo-milestone2`: live demo PRs, marked **do not merge**, kept open as the record of the passing demos.

### Milestone 3 (merged to `main` in #9; see `MILESTONE_3.md`)
Everything below is offline-tested (fixtures, the fake model, and an in-memory GitHub), and the suite passes in CI. **Live so far, at `shadow` (PR #10):** triage classified a docs PR as T0 and reported "would merge"; when CI finished, the **merge gate** fired from `workflow_run`, found the PR, re-checked it without a new model call, and updated the same comment. No writes besides the comment. Not yet live: autofix, real merges, escalation labels, the post-merge guard, and ChatOps. GPT Astral's QA (`docs/qa/2026-10-24-milestone-3.md`) recommends staying at `shadow`/`fix` until those are tested on disposable PRs.

| Phase | State | What it is |
|---|---|---|
| 0. Prerequisites | ✅ Code (labels and repo settings are manual) | `.github/workflows/ci.yml` (check `tests`, also runs on `workflow_dispatch`); `.github/pr-sentinel.yml` at `autonomy: shadow` |
| A. Triage | ✅ | Deterministic signals from the diff: categories, size, Python AST equivalence, secrets, hidden Unicode, test weakening, dependencies, links, intent mismatch. Combined with Claude's triage; Claude can only raise the tier |
| B. Policy | ✅ | Config read from the default branch only; autonomy `off`/`review`/`shadow`/`fix`/`merge`, capped by the `SENTINEL_AUTONOMY` kill switch; decision matrix; every §5.4 merge precondition, recorded in `blocked_by` |
| C. Escalation | ✅ | T3/BLOCK: `sentinel:needs-owner` / `sentinel:blocked` label, review request to owners (skipping the author), @mention, and an owner review guide in the comment. Not repeated unless the reasons change |
| D. Merge | ✅ | SHA-pinned squash merge with a `PR-Sentinel-Merge:` trailer; waits while CI is pending; merge gate (`python -m pr_agent.gate`) on CI completion re-checks everything without a new model call |
| E. Autofix | ✅ | Separate fixer loop proposes a patch; deterministic validator; one fast-forward-only commit via the Git Data API; then dispatches CI (no new secret needed); loop guards (autofix heads are review-only, attempt cap, no retry after red CI, human reverts win) |
| F. Contracts and dashboard | ✅ | Optional `triage`, `autofix`, `actions` in `run_result.json` and the state block; Triage card, actions, would-have, and owner guide on the dashboard |
| G. Post-merge guard | ✅ | `python -m pr_agent.guard`: CI red on `main` after an autonomous merge → revert PR (or escalation if the files changed again) and a `sentinel:paused` issue that stops merges |
| H. ChatOps | ✅ | `/sentinel hold`, `release`, `merge`, `fix`, `recheck`, `explain`, `reclassify <category>`, from people with write access only |
| I. Extras | Partial | `category:*` / `size:*` labels only; no stale sweeper, issue → fix PR, digest or webhook |

### Milestone 2 (merged to `main`; see `MILESTONE_2.md`)
| Phase | State | Offline-tested | Live-tested |
|---|---|---|---|
| A. CI check visibility + failure summary | ✅ Done | ✅ | ✅ PR #6: read the real failing `demo-tests` check, ignored its own check, likely cause matched the crash |
| B. Related-file context (`list_related_files`) | ✅ Done | ✅ | ✅ PR #6: read unchanged `src/pr_agent/state.py` to confirm the helper returns `None`, then flagged the crash line |
| C. Evidence-based escalation | ✅ Done | ✅ | ⏳ Not triggered live yet; both PR #6 runs completed normally |
| D. Static dashboard (`python -m pr_agent.dashboard`) | ✅ Done | ✅ | ✅ PR #6: `dashboard.html` uploaded with the run artifacts |

- **CI (A):** reads check runs for the pinned head SHA only. Each check is `passed`, `failed`, `pending` or `neutral`, and the overall state is `passed`, `failed`, `pending`, `mixed`, `not_found` or `unavailable`. A 403 or failed request is `unavailable`, never a pass. PR Sentinel's own check and checks for other commits are ignored. Failed checks include up to 20 annotations. Claude can add a *likely cause*, labelled as its interpretation, only when a check failed.
- **Context (B):** `list_related_files(path)` returns a changed file's local imports, its tests and same-directory files at the same commit. `read_file` can then open those, and only those (at most 4 per run). Symlinks and files over 1 MB are never offered. Findings in related files never get a line number.
- **Dashboard (D):** a static, self-contained HTML page (no JavaScript) for one run, built from `run_result.json` and `actions.jsonl`. It shows status, findings, CI checks and likely cause, escalation and next step, files inspected, tool calls, and usage against limits. Everything is HTML-escaped. There's no cost estimate and no history: it's one run's snapshot. `--demo-data` labels fixture runs.
- **Escalation (C):** a `needs_human` result now carries a reason code, what was checked, what's unknown, and a next step. The codes are `INSUFFICIENT_EVIDENCE` (a serious finding on a file never inspected), `CONFLICTING_EVIDENCE` (CI fails in a reviewed file, but the review found nothing to explain it), `TOOL_FAILURES`, `REPEATED_TOOL_REQUEST`, `INVALID_TOOL_REQUESTS`, `REVIEW_SCHEMA_INVALID`, `MODEL_DECLINED`, `MODEL_REQUESTED` and `NOTHING_REVIEWABLE`. Budget stops stay `budget_exceeded`, with `LIMIT_REACHED`. Open findings or failing CI on their own still give `completed`.

## Changelog
Newest first. Every commit adds an entry here.

- **2026-10-24 — Triage floor gaps, guard trigger, gate race (#11); merge-gate permission.** (1) Merged #11 (Sidharth Deepak), three real Milestone 3 bugs:
  - **Claude's label could pull a risky change into auto-merge.** E.g. `os.system(...)` called "formatting", or a Makefile `curl … | sh` called "docs". Now `docs`/`comments_only`/`formatting`/`tests_only`/`dependency_patch` count only if the diff shows them; unexplained code changes become `unknown` (T2). Unrecognized file types (Makefile, `.html`, extensionless scripts) set a T2 floor, docs are recognized by extension only, and a changed shebang or `coding:` line counts as a code change.
  - **The post-merge guard could never fire.** Merges made with `GITHUB_TOKEN` start no push workflows, so the executor now dispatches CI on the default branch after a merge, and the guard also listens for dispatched runs.
  - **The merge gate and the review job could run at the same time** (two Claude reviews, duplicate comments). They now share the PR's concurrency group.
  (2) Follow-up: the **merge-gate** job (where most merges happen) lacked `actions: write`, so that CI dispatch would have got a 403 and the guard still wouldn't fire. Added the permission, plus a test that every job that can merge has it. Needs live confirmation: a CI run dispatched with `GITHUB_TOKEN` should still fire `workflow_run` (GitHub's documented exception). 239 tests.
- **2026-10-24 — First live Milestone 3 run; QA report merged (#10); post-merge guard rename fix.** (1) PR #10 ran Milestone 3 live at `shadow`: Triage `docs`/T0, "would merge", then the merge gate re-evaluated it after CI with no new model call and updated the same comment. (2) Merged #10 (SahilSN): GPT Astral's Milestone 3 QA report, 223 tests passed, one high-severity finding. (3) Fixed that finding: when an autonomous merge **renamed** a file, the post-merge guard only checked the new path for later changes but also restored the old one, so a revert PR could overwrite a file a person later recreated at the old path. It now checks both paths and escalates instead. The regression test fails on the old code. (4) Reused reviews (merge gate) now keep the original review's model and token stats instead of showing 0, and the comment says the review text is from the earlier run. 225 tests.
- **2026-10-24 — Milestone 3 merged to `main` (#9).** Squash-merged after team sign-off on the safety-rule change. On #9, CI `tests` passed (223 tests, Python 3.11). Its `PR Sentinel review` check failed only because that PR's new workflow called `main`'s older CLI with a flag it didn't know yet (`--run-url`); that's resolved now that both are on `main`. Live effect: every new same-repo PR gets a **Triage** section and a "Would have" list (`autonomy: shadow`); nothing else is written. The labels exist, and demo PRs #5 and #6 are on `sentinel:hold`. To go further, a person must allow Actions write permissions and PR creation, and set the `SENTINEL_AUTONOMY` repo variable.
- **2026-10-24 — Milestone 3: autonomous triage, fixes and merges (branch `feature/autonomous-triage`).** New modules: `sentinel_config.py` (policy file), `triage.py` (deterministic signals and combining), `policy.py` (gates, decision matrix, merge preconditions), `fixer.py` (fix proposals and patch validation), `executor.py` (the only writer), `maintainer.py` (orchestration), `gate.py` (merge gate), `guard.py` (post-merge revert and pause), and `chatops.py`. The review output and `run_result.json` gain optional `triage`, `bugfix_assessment`, `owner_review_guide`, `autofix` and `actions`; v1/v2 results still validate. The workflow adds the merge gate, post-merge guard and ChatOps jobs, with per-job permissions; it never cancels a run mid-write. `PROJECT.md` and `AGENTS.md` now say autonomous writes happen only through this policy engine, behind the autonomy setting and kill switch, as the team agreed. New dependency: PyYAML. Owner alerts go to SahilSN. Autofix verification uses the CI-dispatch fallback. The end-to-end tests caught one real bug, fixed before commit: the `sentinel:paused` circuit breaker was ignored because its blocker name started with `autonomy_`. Owner @mentions only happen at `fix`/`merge` autonomy, since a mention notifies people. 122 new tests (223 total). Offline only; nothing has run live.
- **2026-10-24 — Milestone 2 live-verified; #7 and #8 merged; truncated file list surfaced.** (1) PR #6 (live demo, not merged) exercised CI evidence, related-file context, single-comment update and the dashboard artifact on real GitHub and Claude. Only escalation wasn't triggered. (2) Merged #7 (Sidharth Deepak): failed checks now show the first *failure-level* annotation instead of a runner warning, annotations are sorted failures-first before truncation, and warnings no longer count as failure locations. (3) Merged #8 (SahilSN): QA report `docs/qa/2026-10-24-milestone-2.md`, no blockers. (4) Follow-up from that report: when GitHub truncates the repository file list, `list_related_files` now says so (`file_list_truncated`) instead of silently offering an incomplete list. 101 tests.
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

### 5. Milestone 3 offline demos (no credentials; fixtures use `autonomy: merge`, but fixture runs can never write)
```bash
python -m pr_agent.cli --fixture tests/fixtures/m3_docs_typo_pr.json --fake-model --dry-run       # T0 docs -> would merge
python -m pr_agent.cli --fixture tests/fixtures/m3_bugfix_autofix_pr.json --fake-model --dry-run  # T1 bug -> validated autofix patch
python -m pr_agent.cli --fixture tests/fixtures/m3_bugfix_ready_pr.json --fake-model --dry-run    # T1 bug fix, CI green -> would merge
python -m pr_agent.cli --fixture tests/fixtures/m3_architecture_pr.json --fake-model --dry-run    # T3 -> owner alert + review guide
python -m pr_agent.cli --fixture tests/fixtures/m3_sneaky_pr.json --fake-model --dry-run          # "docs: typo" that edits CI -> T3
python -m pr_agent.cli --fixture tests/fixtures/m3_secret_pr.json --fake-model --dry-run          # BLOCK; the key is never shown
python -m pr_agent.cli --fixture tests/fixtures/m3_docs_typo_pr.json --fake-model --autonomy shadow  # "Would have" only
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

**Milestone 3 policy** lives in `.github/pr-sentinel.yml`, and only the **default branch's** copy is used. A missing or invalid file means `review` (no writes). The repo variable `SENTINEL_AUTONOMY` caps autonomy: set it to `review` to stop all writes at once. Fixer budget: `MAX_FIX_STEPS=4`, `MAX_FIX_TOKENS=40000`.

**Repo settings needed before going above `shadow`** (people have to do these; see `MILESTONE_3.md` §20):
- Settings → Actions → General → Workflow permissions: allow write permissions, and enable "Allow GitHub Actions to create and approve pull requests" (for revert PRs).
- Repo variable `SENTINEL_AUTONOMY`: start at `shadow`, then `fix`, then `merge` once the demo passes.
- Labels (`sentinel:hold` and friends) are created automatically on first use.

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
| `src/pr_agent/cli.py` | CLI flags, credential checks, idempotency skip, dry-run vs publish; runs the maintainer after the review. |
| `src/pr_agent/sentinel_config.py` | Milestone 3 policy file (default branch only), autonomy levels and kill switch, path globs. |
| `src/pr_agent/triage.py` | Deterministic signals and categories from the diff, and combining with Claude's triage (can only raise the tier). |
| `src/pr_agent/policy.py` | Pure decision engine: PR-state gates, decision matrix, every merge precondition (`blocked_by`). |
| `src/pr_agent/fixer.py` | Fixer loop (proposes patches) and the deterministic patch validator. |
| `src/pr_agent/executor.py` | The only code that writes on PR Sentinel's behalf: labels, review requests, autofix commit, CI dispatch, merge. |
| `src/pr_agent/maintainer.py` | Orchestrates triage → decision → fixer → executor after each review. |
| `src/pr_agent/gate.py` / `guard.py` / `chatops.py` | Merge gate, post-merge revert and pause, `/sentinel` commands. |

The `run_result.json` format is exactly PROJECT.md §6.4, plus one additive field on each finding: `times_seen` (how many reviewed commits have reported it).

## Known limitations
- The review is a single PR conversation comment, not line-level review comments.
- Same-repo PRs only, and check-run / CI-log investigation (P2) is not built.
- No dollar cost shown, only measured token usage.
- `--fake-model` is a few regex rules for demos and tests, not a real reviewer.
- CI is read once, when the review runs. Checks still running show as *pending*, and the comment isn't updated when they finish (it refreshes on the next push). There's no completed-check trigger.
- CI evidence is check summaries and annotations only; raw job logs aren't read.
- Related-file discovery resolves Python imports. For other languages it only offers same-directory files.
- Milestone 3: one workflow job both calls Claude and writes. The recommended split into "decide" and "act" jobs (`MILESTONE_3.md` §15) isn't done. Claude still has no write tools, and every write is decided by deterministic code.
- Milestone 3: autofix pushes use `GITHUB_TOKEN` plus a CI dispatch. That push doesn't trigger a new review, so the merge gate reviews the autofix commit before merging.
- Milestone 3: `callers_checked` in a bug-fix assessment is Claude's claim and isn't verified independently. Loosened test tolerances aren't detected as test weakening.
- Request timeouts are httpx per-phase timeouts (connect/read/write), not a total for the whole response. A server trickling bytes could in theory stretch a call past the runtime ceiling. That's unlikely with the Anthropic API, and the Actions `timeout-minutes: 5` is the outer backstop.

## Team
- Lead implementation (Claude Code)
- Debugging on `fix/*` branches (Claude Code)
- Testing, QA, differentiation (Codex)

Shared agent rules live in `AGENTS.md` / `CLAUDE.md`; personal role instructions go in a gitignored `CLAUDE.local.md`. Debugging work goes on `fix/*` branches and testing work on `test/*` branches, each opened as a PR into `main`.

## Demo
See PROJECT.md §14 for the judge script. Keep a recorded successful run in `demo/` as a backup.
