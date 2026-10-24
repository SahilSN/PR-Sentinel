"""Show which commit PR Sentinel last reviewed, from a saved copy of its PR comment.

    python demo/last_review.py < comment.md
"""

import sys

from pr_agent.state import decode_state


def last_reviewed_commit(comment_body: str) -> str:
    state = decode_state(comment_body)
    if state is None:
        return "(no review yet)"
    return state.head_sha[:12]


if __name__ == "__main__":
    print(last_reviewed_commit(sys.stdin.read()))
