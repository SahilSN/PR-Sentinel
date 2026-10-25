# GPT Astral QA: Milestone 3 Live Autofix Gate

## Reviewed revision

- `origin/main`: `af5b5c27f2e831bf165e74059ed6431379a2c4de`
- Commits reviewed: `7a0a946f14428031239837e26999f24d306fe55d` through `af5b5c27f2e831bf165e74059ed6431379a2c4de`
- Live test: PR #24, autofix head `f639848ffad1f651c080e3b3170cafc2fd43ebd4`

## Verification performed

- Executed the full automated test suite on Python 3.12 with PyYAML 6.0.3: **257 passed**.
- Compiled the Python source tree successfully.
- Confirmed the full merge-precondition re-check and secret-redaction regression coverage by inspection.
- Inspected the live PR #24 comment, Actions runs, dispatched CI result, merge-gate logs, and current PR state read-only.

No GitHub workflow, merge, or repository setting was changed during this QA run.

## Offline result

The under-lock precondition fix is covered for hold labels, requested changes, closed PRs, new pushes, red CI, daily caps, pause state, lock failure, and the unchanged success path. Secret echoes are redacted from the comment, state block, result, and trace artifacts. The complete offline suite passes.

## Finding

### High: a successful autofix CI dispatch does not trigger the merge gate live

PR #24 exercised the real merge/autofix path:

1. The initial head `45a0b2298104715d5931f3480d668648c2e3868b` failed CI.
2. PR Sentinel identified the wrong test assertion and pushed autofix `f639848ffad1f651c080e3b3170cafc2fd43ebd4`.
3. The agent dispatched the `CI` workflow for the branch. Run `40148688197` completed successfully on the autofix SHA.
4. No subsequent `workflow_run` execution of `Autonomous PR Review` was created for that successful dispatched run.
5. PR #24 remained open with no check rollup on the autofix head. The earlier merge-gate run for the failed original SHA correctly found no PR after the head moved, but nothing re-evaluated the new SHA.

The new pull-request runs requested for the bot-authored autofix were also `action_required`, so they did not provide a fallback review or merge gate.

The observed behavior is consistent with GitHub's protections against recursively triggering workflows from operations performed with `GITHUB_TOKEN`, but the finding is based on the live run rather than that inference.

Recommended correction: use a GitHub App or narrowly scoped bot token for the autofix/dispatch path, or explicitly invoke a trusted gate after dispatched CI completion through a mechanism that is verified to generate the follow-up event. Add an end-to-end check that requires the autofix SHA to receive CI evidence and a gate decision.

## Configuration risk

`.github/pr-sentinel.yml` now sets `autonomy: merge`, and the repository has no `SENTINEL_AUTONOMY` variable to cap it. Merge autonomy is therefore active while this live path is unverified and currently stalls after autofix.

## Recommendation

**Set the `SENTINEL_AUTONOMY` repository variable to `shadow` immediately.** Restore `merge` only after the autofix CI-to-gate chain completes live and the autonomous merge plus post-merge guard have both been demonstrated on disposable pull requests.
