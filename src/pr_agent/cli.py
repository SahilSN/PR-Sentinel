"""Command-line entry point, used both locally and by GitHub Actions.

    python -m pr_agent.cli --fixture tests/fixtures/buggy_pr.json --fake-model --dry-run
    python -m pr_agent.cli --repo OWNER/REPO --pr 12 --dry-run
    python -m pr_agent.cli --repo OWNER/REPO --pr 12 --publish
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from . import config
from .agent import Tracer, run_review
from .ci import ci_report_from_fixture, fetch_ci_report
from .claude_client import ClaudeModel, FakeModel
from .github_client import GitHubClient, GitHubError
from .review import new_side_text, render_review_comment
from .schemas import ChangedFile, PreviousReview, PullRequestInfo, ReviewResult
from .state import encode_state, get_previous_bot_review, resolve_bot_login, upsert_review_comment
from .tool_registry import RepoEntry

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pr_agent", description="Autonomous GitHub PR reviewer (PR Sentinel).")
    src = p.add_argument_group("input (choose one)")
    src.add_argument("--fixture", type=Path, help="offline JSON fixture (see tests/fixtures)")
    src.add_argument("--repo", help="OWNER/REPO of a live pull request")
    src.add_argument("--pr", type=int, help="pull request number")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the review, never write to GitHub (default)")
    mode.add_argument("--publish", action="store_true", help="create/update the bot comment on the PR")
    p.add_argument("--fake-model", action="store_true", help="use the offline heuristic model instead of Claude")
    p.add_argument("--force", action="store_true", help="re-review even if this head SHA was already reviewed")
    p.add_argument("--no-ci", action="store_true", help="skip reading CI check runs for the head commit")
    p.add_argument("--max-steps", type=int, help="override MAX_AGENT_STEPS (e.g. 1 to demo a budget stop)")
    p.add_argument("--output-dir", type=Path, default=Path("artifacts"), help="where run_result.json/actions.jsonl go")
    return p.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.fixture and (args.repo or args.pr):
        raise UsageError("use either --fixture or --repo/--pr, not both")
    if not args.fixture and not (args.repo and args.pr):
        raise UsageError("provide --fixture PATH, or both --repo OWNER/REPO and --pr NUMBER")
    if args.fixture and args.publish:
        raise UsageError("--publish needs a live PR (--repo/--pr); fixtures are dry-run only")
    if args.max_steps is not None and args.max_steps < 1:
        raise UsageError("--max-steps must be >= 1")
    if args.repo and not config.github_token():
        raise UsageError("GITHUB_TOKEN is not set (needed to read the PR)")
    if not args.fake_model and not config.has_anthropic_credentials():
        raise UsageError("ANTHROPIC_API_KEY is not set; set it or pass --fake-model for an offline run")


def load_fixture(path: Path) -> tuple[PullRequestInfo, list[ChangedFile], PreviousReview | None, dict, dict | None]:
    """Fixture keys: the PROJECT.md §6.3 input, plus optional `file_contents` ({path: text} at the head commit,
    which also acts as the repository file list) and optional `ci` (see ci.ci_report_from_fixture)."""
    data = json.loads(path.read_text())
    pr = PullRequestInfo(**{k: data[k] for k in ("repo", "pr_number", "title", "base_sha", "head_sha")})
    files = [ChangedFile(**f) for f in data["changed_files"]]
    previous = PreviousReview.model_validate(data["previous_review"]) if data.get("previous_review") else None
    return pr, files, previous, data.get("file_contents") or {}, data.get("ci")


def write_result(result: ReviewResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "run_result.json"
    path.write_text(result.model_dump_json(indent=2) + "\n")
    return path


def main(argv: list[str] | None = None, gh: GitHubClient | None = None, model=None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        settings = config.Settings.from_env()
    except (UsageError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.max_steps:
        settings.limits.max_agent_steps = args.max_steps
    model = model or (FakeModel() if args.fake_model else ClaudeModel(settings))
    tracer = Tracer(args.output_dir / "actions.jsonl")

    try:
        if args.fixture:
            pr, files, previous, contents, ci_data = load_fixture(args.fixture)
            patches = {f.path: f.patch for f in files}
            read_source = lambda path: contents.get(path) or new_side_text(patches.get(path))  # noqa: E731
            list_repo_files = lambda: [RepoEntry(p, len(c)) for p, c in contents.items()] + [  # noqa: E731
                RepoEntry(f.path) for f in files if f.path not in contents]
            ci = None if args.no_ci or ci_data is None else ci_report_from_fixture(ci_data, pr.head_sha, settings)
            comment_id, bot_login = None, None
        else:
            gh = gh or GitHubClient(config.github_token())
            pr = gh.get_pull_request(args.repo, args.pr)
            files = gh.get_changed_files(args.repo, args.pr)
            bot_login = resolve_bot_login(gh, settings.bot_login)
            previous = get_previous_bot_review(gh, args.repo, args.pr, bot_login)
            comment_id = previous.comment_id if previous else None
            if previous and not previous.head_sha:
                previous = None  # our comment exists but its state was unreadable: update it, don't trust it
            if previous and previous.head_sha == pr.head_sha and previous.status == "completed" and not args.force:
                print(f"Commit {pr.head_sha[:12]} already has a completed review; skipping (use --force to redo).")
                tracer.log("skipped", repo=pr.repo, pr_number=pr.pr_number, head_sha=pr.head_sha)
                return EXIT_OK
            read_source = lambda path: gh.read_file(args.repo, path, pr.head_sha)  # noqa: E731  pinned SHA
            def list_repo_files() -> tuple[list[RepoEntry], bool]:  # blobs only: no symlinks or submodules
                entries, truncated = gh.get_tree(args.repo, pr.head_sha)
                return [RepoEntry(e["path"], e.get("size")) for e in entries
                        if e.get("type") == "blob" and e.get("mode") != "120000"], truncated
            ci = None if args.no_ci else fetch_ci_report(gh, args.repo, pr.head_sha, settings)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (OSError, KeyError, ValueError, ValidationError) as exc:
        print(f"error: could not load input: {exc}", file=sys.stderr)
        return EXIT_USAGE

    result = run_review(pr, files, previous, model=model, read_source=read_source,
                        limits=settings.limits, tracer=tracer, ci=ci, list_repo_files=list_repo_files)
    result_path = write_result(result, args.output_dir)

    if args.publish:
        body = render_review_comment(result, encode_state(result))
        try:
            posted = upsert_review_comment(gh, pr.repo, pr.pr_number, body, bot_login, known_comment_id=comment_id)
        except GitHubError as exc:
            print(f"error: review finished but publishing failed: {exc}", file=sys.stderr)
            return EXIT_FAILED
        tracer.log("published", comment_id=posted)
        print(f"Published review comment {posted} on {pr.repo}#{pr.pr_number}.")
    else:
        print(render_review_comment(result))
        print("\n(dry run: nothing was posted to GitHub)")

    print(f"Status: {result.status}. Result written to {result_path}", file=sys.stderr)
    return EXIT_FAILED if result.status == "error" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
