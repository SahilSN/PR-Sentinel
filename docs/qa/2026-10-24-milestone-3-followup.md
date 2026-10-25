# GPT Astral QA Follow-up: Milestone 3 Safety Fixes

## Reviewed revision

- `origin/main`: `4920c51e86f8d88a9ac27585adac1432400e02e3`
- Commits reviewed: `79a8ab203b96b56c15c1bc9e3eba161b72c783fd` through `4920c51e86f8d88a9ac27585adac1432400e02e3`

## Verification performed

- Executed the full automated test suite on Python 3.12 with PyYAML 6.0.3: **239 passed**.
- Compiled the Python source tree successfully.
- Confirmed the rename guard now checks both the new and previous paths and has a regression test.
- Inspected the deterministic triage hardening, dispatched post-merge CI path, workflow permissions, merge-gate routing, stored usage statistics, and concurrency configuration.

No live GitHub writes, fixes, merges, or revert operations were performed during this QA run.

## Resolved findings

- The rename-aware post-merge guard issue from the first Milestone 3 QA report is fixed and regression-tested.
- Claude can no longer classify an otherwise unexplained code change as a diff-only low-risk category to make it auto-mergeable.
- Autonomous merges now dispatch default-branch CI, and all jobs capable of merging have `actions: write` for that dispatch.
- The review and merge-gate runs for the same PR now share a concurrency group.

## Finding

### High: autonomous merges from different PRs are not globally serialized

Milestone 3 requires every merge-capable run to share a global `sentinel-merge` concurrency group. The workflow instead uses a per-PR group for `review` and a per-PR-or-commit group for `merge-gate`. Two different PRs can therefore execute merge decisions and writes concurrently.

Consequences include:

1. Both runs can observe the same daily merge count before either merge is labeled, allowing the configured cap to be exceeded.
2. Default-branch CI dispatches and post-merge attribution can overlap, making rollback diagnosis less reliable when one of the merged changes breaks main.
3. The implementation no longer satisfies the documented "Serialize merges" safety invariant.

Recommended correction: move merge execution into a globally serialized job or otherwise provide an atomic repository-wide merge lock while retaining per-PR serialization for review and state updates. Add a workflow-level regression test that requires every merge writer to pass through the global merge group.

## Recommendation

**Shadow/fix-mode demo: ready. Autonomous merge mode: blocked.** Keep `SENTINEL_AUTONOMY` below `merge` until repository-wide merge serialization is restored and the merge, post-merge CI dispatch, and guard paths are live-tested on disposable pull requests.
