# Runtime hard-ceiling QA

## Scope

Tested commit `397d232b07fa1d8a47507b69a63828e59950838c`, including the prompt-envelope hardening, repeated-finding accounting, and `MAX_RUNTIME_SECONDS` request limiting introduced by PR #2 and its follow-up.

## Results

- Full automated suite: 43 passed on Python 3.12.
- Source and test compilation: passed.
- Offline prompt-injection fixture: passed in dry-run mode; no GitHub write occurred.
- Remaining-runtime boundary: known failure documented by `test_request_timeout_never_exceeds_remaining_runtime`.
- Live Claude API, live GitHub publishing, and end-to-end Actions execution: not tested.

## Bugs

### Major — request timeout can exceed the advertised hard runtime ceiling

Reproduction:

1. Call `request_limits(1.0)` with one second remaining.
2. Observe that it returns a five-second timeout and zero retries.
3. The allowed request duration can therefore exceed the remaining run budget by four seconds.

Expected: the request's worst-case allowed duration never exceeds the positive remaining runtime.

Actual: `request_limits()` imposes a five-second minimum timeout, so the documented hard ceiling is not strictly enforced near the deadline. HTTP phase timeouts may also differ from a total wall-clock deadline and should be verified against the SDK behavior.

## Quality notes

- Escaping angle brackets in serialized PR data closes the identified tag-breakout path and has a focused regression test.
- Distinct-head accounting for `times_seen` is covered by a regression test.
- The runtime feature should avoid claiming a hard ceiling until an outer deadline mechanism or a timeout value bounded by the actual remaining time is used.
- No live credentials were used during this QA pass.

## Recommendation

Demo-ready: **No**. Fix or accurately document the runtime ceiling, then complete a live same-repository PR review and comment-update rehearsal.
