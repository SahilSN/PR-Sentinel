# GPT Astral QA: Milestone 3 Merge-Lock Re-check

## Reviewed revision

- `origin/main`: `c095b6c981c890934add6e4e986a7ebbdb6b4779`
- Commits reviewed: `b1cdd8341dd6eebb3e3f19c0a6a1f20f935cc7a6` through `c095b6c981c890934add6e4e986a7ebbdb6b4779`

## Verification performed

- Executed the full automated test suite on Python 3.12 with PyYAML 6.0.3: **247 passed**.
- Compiled the Python source tree successfully.
- Inspected lock acquisition, stale-lock takeover, release behavior, merge execution, daily-cap recounting, pause handling, post-merge actions, and the re-check/state fixes.

No live GitHub writes or merges were performed during this QA run.

## Resolved findings

- Autonomous merge execution is now protected by an atomic repository-wide Git ref lock.
- The daily merge count and `sentinel:paused` circuit breaker are refreshed while holding that lock.
- The lock is released after successful, blocked, and failed merge attempts; stale holders have a bounded takeover path.
- Shadow-mode escalation text and retained autofix previews now match the actions actually taken or proposed.

## Finding

### High: most merge preconditions are not refreshed after waiting for the lock

`run_maintainer` evaluates the full merge gate before acquiring the repository-wide lock. Lock acquisition may then wait for up to 90 seconds. Once the lock is held, `fresh_merge_blockers` refreshes only the daily merge count and the `sentinel:paused` issue.

During that interval, a person can add a hold label, request changes, convert the PR to draft, close it, or otherwise change a merge precondition. CI or mergeability can also change. None of those facts is fetched again under the lock. The SHA passed to GitHub prevents merging a changed head, but it does not protect the other mutable preconditions.

This means the agent can merge after a human has explicitly stopped or rejected the PR, contrary to the documented requirement to re-check every merge precondition using fresh GitHub data.

Recommended correction: while holding the lock, reload the PR, labels, reviews, CI checks, trust facts, mergeability, and policy state, then run the complete `merge_blockers` calculation again against the reviewed SHA. Add a regression test that changes a hold label or review state while the runner waits for the lock and verifies that no merge occurs.

## Recommendation

**Shadow/fix mode: ready. Autonomous merge mode: blocked.** Keep `SENTINEL_AUTONOMY` below `merge` until the full merge gate is re-evaluated under the repository-wide lock and the path is live-tested.
