# PR Sentinel

> An autonomous GitHub assistant that reviews pull requests with Claude, follows up when new commits land, remembers what it found, and stops (or asks for a human) when it can't make progress.

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

`ANTHROPIC_FALLBACKS=on` (the default) enables the API's server-side refusal fallback on supported models. If a safety classifier declines the request, the API retries it on a fallback model instead of failing. Set it to `off` to disable.

## Code map and contracts for teammates

| Module | Responsibility |
|---|---|
| `src/pr_agent/schemas.py` | **Shared contracts**: `PullRequestInfo`, `ChangedFile`, `PreviousReview`, `ReviewResult` (= `run_result.json`). Coordinate before changing these. |
| `src/pr_agent/github_client.py` | GitHub REST wrapper: `get_pull_request`, `get_changed_files`, `read_file(repo, path, ref)`, comments. Pagination, retries, no tokens in errors. |
| `src/pr_agent/agent.py` | Controller: tool-use loop, limits, stop reasons, repair-once validation, trace. Entry: `run_review(pr, files, previous, model=..., read_source=..., limits=...)`. |
| `src/pr_agent/tool_registry.py` | The two allowlisted read-only tools, path validation, file selection and caps. |
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

## Team
- Lead implementation (Claude Code)
- Debugging on `fix/*` branches (Claude Code)
- Testing, QA, differentiation (Codex)

Shared agent rules live in `AGENTS.md` / `CLAUDE.md`; personal role instructions go in a gitignored `CLAUDE.local.md`.

## Demo
See PROJECT.md §14 for the judge script. Keep a recorded successful run in `demo/` as a backup.
