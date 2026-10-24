"""Cross-run memory: one bot comment per PR holds the rendered review plus a machine-readable state block."""

from __future__ import annotations

import base64
import json
import re

from pydantic import ValidationError

from .github_client import GitHubClient
from .schemas import PreviousReview, ReviewResult

MARKER = "<!-- pr-sentinel-review:v1 -->"
STATE_PREFIX = "<!-- pr-sentinel-state:v1 "
STATE_RE = re.compile(r"<!-- pr-sentinel-state:v1 ([A-Za-z0-9+/=]+) -->")
MAX_STATE_BYTES = 32_000
DEFAULT_BOT_LOGIN = "github-actions[bot]"


def encode_state(result: ReviewResult) -> str:
    """Base64 keeps the JSON from ever containing `-->` or anything Markdown would render."""
    state = PreviousReview(
        head_sha=result.head_sha,
        status=result.status,
        open_findings=result.open_findings,
        resolved_findings=result.resolved_findings[-20:],
    )
    payload = base64.b64encode(state.model_dump_json().encode()).decode()
    if len(payload) > MAX_STATE_BYTES:
        state.resolved_findings = []
        payload = base64.b64encode(state.model_dump_json().encode()).decode()
    return f"{STATE_PREFIX}{payload} -->"


def decode_state(body: str) -> PreviousReview | None:
    """Parse the state block. Treat it as untrusted: anything malformed is ignored, not trusted."""
    match = STATE_RE.search(body)
    if not match or len(match.group(1)) > MAX_STATE_BYTES:
        return None
    try:
        return PreviousReview.model_validate(json.loads(base64.b64decode(match.group(1), validate=True)))
    except (ValueError, ValidationError):
        return None


def resolve_bot_login(gh: GitHubClient, configured: str | None) -> str:
    return configured or gh.get_authenticated_login() or DEFAULT_BOT_LOGIN


def find_bot_comment(comments: list[dict], bot_login: str) -> dict | None:
    """Our comment = has the marker AND is authored by the bot. A user pasting the marker doesn't count."""
    ours = [
        c
        for c in comments
        if MARKER in (c.get("body") or "") and (c.get("user") or {}).get("login", "").lower() == bot_login.lower()
    ]
    return ours[0] if ours else None


def get_previous_bot_review(gh: GitHubClient, repo: str, pr_number: int, bot_login: str) -> PreviousReview | None:
    comment = find_bot_comment(gh.list_issue_comments(repo, pr_number), bot_login)
    if comment is None:
        return None
    previous = decode_state(comment.get("body") or "")
    if previous is None:
        # Marker exists but state is unreadable: remember the comment so we still update instead of duplicating.
        return PreviousReview(comment_id=comment["id"], head_sha="", status="error")
    previous.comment_id = comment["id"]
    return previous


def upsert_review_comment(
    gh: GitHubClient, repo: str, pr_number: int, body: str, bot_login: str, known_comment_id: int | None = None
) -> int:
    """Update our existing comment if there is one; otherwise create it. Re-checks right before writing."""
    comment_id = known_comment_id
    if comment_id is None:
        existing = find_bot_comment(gh.list_issue_comments(repo, pr_number), bot_login)
        comment_id = existing["id"] if existing else None
    if comment_id is not None:
        return gh.update_comment(repo, comment_id, body)
    return gh.create_comment(repo, pr_number, body)
