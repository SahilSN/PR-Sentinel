"""Command-line entry point, used both locally and by GitHub Actions.

    python -m pr_agent.cli --fixture tests/fixtures/buggy_pr.json --fake-model --dry-run
    python -m pr_agent.cli --repo OWNER/REPO --pr 12 --dry-run
    python -m pr_agent.cli --repo OWNER/REPO --pr 12 --publish
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from . import config
from .agent import Tracer, run_review
from .ci import ci_report_from_fixture, fetch_ci_report
from .claude_client import ClaudeModel, FakeModel
from .github_client import GitHubClient, GitHubError
from .review import new_side_text, render_review_comment
from .schemas import ChangedFile, PreviousReview, PullRequestInfo, ReviewResult
from .executor import Executor, tree_modes
from .maintainer import Sources, facts_from_fixture, facts_from_github, run_maintainer
from .policy import PRFacts
from .schemas import CATEGORIES
from .sentinel_config import AUTONOMY_ORDER, CONFIG_PATH, effective_autonomy, parse_config
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
    m3 = p.add_argument_group("Milestone 3 (autonomous triage)")
    m3.add_argument("--autonomy", choices=AUTONOMY_ORDER, help="cap autonomy for this run (can only lower it)")
    m3.add_argument("--gate", action="store_true", help="merge-gate run after CI completes: re-check, never autofix")
    m3.add_argument("--owner-merge", action="store_true", help="owner approved merging (/sentinel merge)")
    m3.add_argument("--force-fix", action="store_true", help="owner asked for a fix (/sentinel fix)")
    m3.add_argument("--reclassify", choices=CATEGORIES, help="owner category override (/sentinel reclassify)")
    m3.add_argument("--config", type=Path, help="policy config file (fixture runs only; live runs read the default branch)")
    m3.add_argument("--run-url", help="link to this Actions run, included in merge commit messages")
    return p.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.fixture and (args.repo or args.pr):
        raise UsageError("use either --fixture or --repo/--pr, not both")
    if not args.fixture and not (args.repo and args.pr):
        raise UsageError("provide --fixture PATH, or both --repo OWNER/REPO and --pr NUMBER")
    if args.fixture and args.publish:
        raise UsageError("--publish needs a live PR (--repo/--pr); fixtures are dry-run only")
    if args.config and not args.fixture:
        raise UsageError("--config is only for fixture runs; live runs read .github/pr-sentinel.yml from the default branch")
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


def fixture_config_text(data: dict, path: Path | None) -> str | None:
    if path is not None:
        return path.read_text()
    cfg = data.get("sentinel_config")
    if cfg is None:
        return None
    return cfg if isinstance(cfg, str) else yaml.safe_dump(cfg)


def result_from_state(previous: PreviousReview, pr: PullRequestInfo, ci) -> ReviewResult:
    """Re-evaluate an already-reviewed commit without a new model call (merge gate, label changes)."""
    return ReviewResult(
        repo=pr.repo, pr_number=pr.pr_number, head_sha=pr.head_sha, status=previous.status,
        summary=previous.summary or "Re-evaluated from the stored review of this commit.",
        open_findings=previous.open_findings, resolved_findings=previous.resolved_findings,
        unreviewed_files=previous.unreviewed_files, ci=ci,
        stop_reason=None if previous.status == "completed" else "Stored review did not complete.",
    )


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
    reuse = False

    try:
        if args.fixture:
            data = json.loads(args.fixture.read_text())
            pr, files, previous, contents, ci_data = load_fixture(args.fixture)
            patches = {f.path: f.patch for f in files}
            base_contents = data.get("base_file_contents") or {}
            read_source = lambda path: contents.get(path) or new_side_text(patches.get(path))  # noqa: E731
            list_repo_files = lambda: [RepoEntry(p, len(c)) for p, c in contents.items()] + [  # noqa: E731
                RepoEntry(f.path) for f in files if f.path not in contents]
            ci = None if args.no_ci or ci_data is None else ci_report_from_fixture(ci_data, pr.head_sha, settings)
            comment_id, bot_login = None, None
            cfg, warning = parse_config(fixture_config_text(data, args.config))
            facts = facts_from_fixture(data, pr)
            sources = Sources(base_text=base_contents.get, head_text=contents.get, list_repo_files=list_repo_files)
            autonomy = effective_autonomy(cfg.autonomy, os.environ.get("SENTINEL_AUTONOMY"), args.autonomy)
        else:
            gh = gh or GitHubClient(config.github_token())
            gh.read_only = not args.publish  # --dry-run: zero writes, enforced in the client
            pr = gh.get_pull_request(args.repo, args.pr)
            files = gh.get_changed_files(args.repo, args.pr)
            bot_login = resolve_bot_login(gh, settings.bot_login)
            previous = get_previous_bot_review(gh, args.repo, args.pr, bot_login)
            comment_id = previous.comment_id if previous else None
            if previous and not previous.head_sha:
                previous = None  # our comment exists but its state was unreadable: update it, don't trust it

            # Policy config comes from the DEFAULT branch, never from the PR.
            try:
                default_branch = gh.get_repo(args.repo).get("default_branch") or "main"
                try:
                    cfg_text = gh.read_file(args.repo, CONFIG_PATH, default_branch)
                except GitHubError as exc:
                    if exc.status != 404:
                        raise
                    cfg_text = None
                cfg, warning = parse_config(cfg_text)
            except GitHubError as exc:
                default_branch = "main"
                cfg, warning = parse_config(None)
                warning = f"Could not read the policy config ({exc.status}); running in review-only mode."
            autonomy = effective_autonomy(cfg.autonomy, os.environ.get("SENTINEL_AUTONOMY"), args.autonomy)
            if autonomy == "off":
                print("PR Sentinel autonomy is off; nothing to do.")
                return EXIT_OK

            already = previous and previous.head_sha == pr.head_sha and previous.status == "completed"
            if already and not args.force:
                if autonomy == "review" and not args.gate:
                    print(f"Commit {pr.head_sha[:12]} already has a completed review; skipping (use --force to redo).")
                    tracer.log("skipped", repo=pr.repo, pr_number=pr.pr_number, head_sha=pr.head_sha)
                    return EXIT_OK
                reuse = True  # re-decide from the stored review: no new model call
            read_source = lambda path: gh.read_file(args.repo, path, pr.head_sha)  # noqa: E731  pinned SHA

            def list_repo_files() -> tuple[list[RepoEntry], bool]:  # blobs only: no symlinks or submodules
                entries, truncated = gh.get_tree(args.repo, pr.head_sha)
                return [RepoEntry(e["path"], e.get("size")) for e in entries
                        if e.get("type") == "blob" and e.get("mode") != "120000"], truncated

            def fetch(sha):
                def read(path):
                    try:
                        return gh.read_file(args.repo, path, sha)
                    except GitHubError:
                        return None
                return read

            ci = None if args.no_ci else fetch_ci_report(gh, args.repo, pr.head_sha, settings)
            if autonomy == "review":
                facts = PRFacts(head_sha=pr.head_sha, title=pr.title, default_branch=default_branch)
            else:
                facts = facts_from_github(gh, args.repo, gh.get_pull(args.repo, args.pr), cfg, bot_login,
                                          default_branch)
            sources = Sources(base_text=fetch(pr.base_sha), head_text=fetch(pr.head_sha),
                              list_repo_files=list_repo_files,
                              modes=lambda: tree_modes(gh, args.repo, pr.head_sha))
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (OSError, KeyError, ValueError, ValidationError) as exc:
        print(f"error: could not load input: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if reuse:
        result = result_from_state(previous, pr, ci)
        tracer.log("reused_review", head_sha=pr.head_sha, gate=args.gate)
    else:
        result = run_review(pr, files, previous, model=model, read_source=read_source,
                            limits=settings.limits, tracer=tracer, ci=ci, list_repo_files=list_repo_files)

    executor = Executor(gh=None if args.fixture else gh, repo=pr.repo, pr_number=pr.pr_number, autonomy=autonomy,
                        can_write=bool(args.publish), run_id=tracer.run_id)
    result, extras = run_maintainer(
        pr=pr, files=files, result=result, previous=previous, facts=facts, config=cfg, config_warning=warning,
        autonomy=autonomy, executor=executor, model=None if reuse else model, read_source=read_source,
        sources=sources, limits=settings.limits, tracer=tracer, gate_mode=args.gate or reuse,
        owner_override=args.owner_merge, force_fix=args.force_fix, reclassify=args.reclassify, run_url=args.run_url)
    result_path = write_result(result, args.output_dir)

    # @mentions notify people, so they're an owner alert: only at autonomies that may alert the owner.
    mention = cfg.owners if autonomy in ("fix", "merge") else []
    if args.publish:
        body = render_review_comment(result, encode_state(result, extras, previous), owners=mention)
        try:
            posted = upsert_review_comment(gh, pr.repo, pr.pr_number, body, bot_login, known_comment_id=comment_id)
        except GitHubError as exc:
            print(f"error: review finished but publishing failed: {exc}", file=sys.stderr)
            return EXIT_FAILED
        tracer.log("published", comment_id=posted)
        print(f"Published review comment {posted} on {pr.repo}#{pr.pr_number}.")
    else:
        print(render_review_comment(result, owners=mention))
        print("\n(dry run: nothing was posted to GitHub)")

    decision = result.triage.decision if result.triage else "review_only"
    print(f"Status: {result.status}. Decision: {decision} (autonomy {autonomy}). Result written to {result_path}",
          file=sys.stderr)
    return EXIT_FAILED if result.status == "error" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
