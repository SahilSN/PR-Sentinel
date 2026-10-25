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


def encode_state(result: ReviewResult, extras=None, previous: PreviousReview | None = None) -> str:
    """Base64 keeps the JSON from ever containing `-->` or anything Markdown would render.

    `extras` (maintainer.StateExtras) carries Milestone 3 state; actions keep the last 20 across runs."""
    actions = ((previous.actions if previous else []) + list(result.actions))[-20:]
    state = PreviousReview(
        head_sha=result.head_sha,
        status=result.status,
        open_findings=result.open_findings,
        resolved_findings=result.resolved_findings[-20:],
        summary=result.summary[:2000],
        unreviewed_files=result.unreviewed_files[:50],
        triage=result.triage,
        decision=result.triage.decision if result.triage else None,
        autofix_attempts=extras.autofix_attempts if extras else 0,
        failed_fix_ids=extras.failed_fix_ids if extras else [],
        human_override=extras.human_override if extras else False,
        actions=actions,
    )
    payload = base64.b64encode(state.model_dump_json().encode()).decode()
    # Over the size cap: drop the least important history first.
    for trim in ("actions", "resolved_findings", "signals"):
        if len(payload) <= MAX_STATE_BYTES:
            break
        if trim == "signals" and state.triage:
            state.triage.signals = state.triage.signals[:10]
        elif trim != "signals":
            setattr(state, trim, [])
        payload = base64.b64encode(state.model_dump_json().encode()).decode()
    return f"{STATE_PREFIX}{payload} -->"


def decode_state(body: str) -> PreviousReview | None:
    """Parse the state block. Treat it as untrusted: anything malformed is ignored, not trusted."""
    # Our block is always last; anything earlier (e.g. inside a quoted patch preview) is ignored.
    found = STATE_RE.findall(body)
    if not found or len(found[-1]) > MAX_STATE_BYTES:
        return None
    try:
        return PreviousReview.model_validate(json.loads(base64.b64decode(found[-1], validate=True)))
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
