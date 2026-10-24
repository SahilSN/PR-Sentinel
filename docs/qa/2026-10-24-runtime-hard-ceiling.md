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

## Follow-up verification

Retested at commit `3609403ab86b24723787ca088c39ed8933c425be` after PR #3 and its follow-up:

- Full automated suite: 46 passed on Python 3.12.
- Source and test compilation: passed.
- The five-second timeout floor and `request_limits()` helper were removed.
- Requests are no longer started when at most one second remains, and each attempt receives no more than the calculated remaining budget.
- Retry coverage now includes overloaded, rate-limited, timeout, and non-retryable client-error cases.
- A live Claude request and real PR read completed on PR #3. Comment publication failed with 403; `pull-requests: write` was added but still needs confirmation on a subsequent PR.

The reported boundary defect is **resolved**. The documented residual limitation remains: HTTP client timeouts are per phase rather than a guaranteed whole-response deadline, with the five-minute Actions job timeout acting as the outer backstop.

Updated recommendation: **Not yet demo-ready** until comment creation and same-comment update are demonstrated on a live PR.
