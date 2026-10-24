"""Tests for demo/last_review.py, run by .github/workflows/demo-tests.yml.

A failing test is reported as a GitHub annotation at the line that raised, so it shows on the check run.
"""

import os
import sys
import traceback

sys.path[:0] = ["src", "demo"]

from last_review import last_reviewed_commit  # noqa: E402


def test_comment_without_state_block():
    assert last_reviewed_commit("Thanks, looks good to me!") == "(no review yet)"


if __name__ == "__main__":
    failures = 0
    for name, test in [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]:
        try:
            test()
            print(f"PASS {name}")
        except Exception as exc:
            failures += 1
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            print(f"::error file={os.path.relpath(frame.filename)},line={frame.lineno},title={name}::"
                  f"{type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
