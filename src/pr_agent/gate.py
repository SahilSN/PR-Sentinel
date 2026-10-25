"""Merge gate: runs when the CI workflow completes for a PR commit, and finishes a merge the review decided on.

    python -m pr_agent.gate --repo OWNER/REPO --sha HEAD_SHA [--publish] [--run-url URL]

It finds the single open same-repo PR whose head is exactly that commit, then re-runs the decision with fresh
GitHub data (CI, mergeability, labels, reviews). It reuses the stored review when this commit was already
reviewed, and only calls Claude when it wasn't (an autofix pushed with GITHUB_TOKEN gets no review of its own).
"""

from __future__ import annotations

import argparse
import sys

from . import cli, config
from .github_client import GitHubClient, GitHubError


def find_pr(gh: GitHubClient, repo: str, sha: str) -> tuple[int | None, str]:
    pulls = [p for p in gh.pulls_for_commit(repo, sha)
             if p.get("state") == "open" and (p.get("head") or {}).get("sha") == sha
             and ((p.get("head") or {}).get("repo") or {}).get("full_name") == repo]
    if len(pulls) != 1:
        return None, f"expected exactly one open PR with head {sha[:7]}, found {len(pulls)}; nothing to do"
    return pulls[0]["number"], ""


def main(argv: list[str] | None = None, gh: GitHubClient | None = None, model=None) -> int:
    p = argparse.ArgumentParser(prog="pr_agent.gate")
    p.add_argument("--repo", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--fake-model", action="store_true")
    p.add_argument("--run-url")
    p.add_argument("--output-dir", default="artifacts")
    args = p.parse_args(argv)
    if not config.github_token() and gh is None:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return cli.EXIT_USAGE
    gh = gh or GitHubClient(config.github_token())
    try:
        number, why = find_pr(gh, args.repo, args.sha)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return cli.EXIT_FAILED
    if number is None:
        print(why)
        return cli.EXIT_OK
    forwarded = ["--repo", args.repo, "--pr", str(number), "--gate", "--output-dir", args.output_dir,
                 "--publish" if args.publish else "--dry-run"]
    if args.fake_model:
        forwarded.append("--fake-model")
    if args.run_url:
        forwarded += ["--run-url", args.run_url]
    return cli.main(forwarded, gh=gh, model=model)


if __name__ == "__main__":
    sys.exit(main())
