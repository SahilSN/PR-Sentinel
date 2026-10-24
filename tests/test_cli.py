import json

from pr_agent.claude_client import FakeModel
from pr_agent.cli import EXIT_OK, EXIT_USAGE, main
from pr_agent.state import MARKER, decode_state

from conftest import FIXTURES, FakeGitHub, load_fixture


def test_offline_fixture_run_needs_no_credentials(clean_env, tmp_path, capsys):
    code = main(["--fixture", str(FIXTURES / "buggy_pr.json"), "--fake-model", "--dry-run", "--output-dir", str(tmp_path)])
    assert code == EXIT_OK
    result = json.loads((tmp_path / "run_result.json").read_text())
    assert result["schema_version"] == 1 and result["status"] == "completed"
    assert len(result["open_findings"]) == 1
    assert (tmp_path / "actions.jsonl").read_text().count("\n") >= 4
    assert "dry run" in capsys.readouterr().out


def test_contradictory_and_missing_flags_are_rejected(clean_env, tmp_path, capsys):
    fixture = str(FIXTURES / "buggy_pr.json")
    assert main(["--fixture", fixture, "--fake-model", "--publish"]) == EXIT_USAGE
    assert main(["--fixture", fixture, "--repo", "o/r", "--pr", "1", "--fake-model"]) == EXIT_USAGE
    assert main(["--repo", "o/r", "--pr", "1", "--fake-model"]) == EXIT_USAGE  # no GITHUB_TOKEN
    assert main(["--fixture", fixture]) == EXIT_USAGE  # no ANTHROPIC_API_KEY and no --fake-model
    assert main([]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err and "ANTHROPIC_API_KEY" in err


# Spec test 11
def test_dry_run_never_writes_to_github(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    fake = FakeGitHub(load_fixture("buggy_pr.json"))
    code = main(["--repo", "demo/agent-playground", "--pr", "12", "--fake-model", "--output-dir", str(tmp_path)],
                gh=fake.client(), model=FakeModel())
    assert code == EXIT_OK
    assert fake.writes == []
    assert any("/contents/app/auth.py" in r.url.path and r.url.params["ref"] == "head456" for r in fake.requests)


# Spec test 8 (end to end) + Flow B
def test_publish_twice_same_head_makes_one_comment_then_updates_on_new_head(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    fake = FakeGitHub(load_fixture("buggy_pr.json"))
    args = ["--repo", "demo/agent-playground", "--pr", "12", "--fake-model", "--publish", "--output-dir", str(tmp_path)]

    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert len(fake.comments) == 1 and MARKER in fake.comments[0]["body"]

    writes_before = len(fake.writes)
    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK  # same SHA: skipped
    assert len(fake.writes) == writes_before and len(fake.comments) == 1

    # Developer pushes the fix: same PR, new head SHA.
    fixed = load_fixture("fixed_pr.json")
    fake.fixture = {**fixed, "previous_review": None}
    assert main(args, gh=fake.client(), model=FakeModel()) == EXIT_OK
    assert len(fake.comments) == 1
    state = decode_state(fake.comments[0]["body"])
    assert state.head_sha == "head789"
    assert state.open_findings == []
    assert [f.id for f in state.resolved_findings] == ["app/auth.py:null-deref"]
    assert "Resolved since last review" in fake.comments[0]["body"]


def test_spoofed_marker_comment_is_not_updated(clean_env, tmp_path):
    clean_env.setenv("GITHUB_TOKEN", "x")
    fake = FakeGitHub(load_fixture("buggy_pr.json"))
    fake.comments.append({"id": 1, "body": MARKER + " I am the bot now", "user": {"login": "mallory"}})
    main(["--repo", "demo/agent-playground", "--pr", "12", "--fake-model", "--publish", "--output-dir", str(tmp_path)],
         gh=fake.client(), model=FakeModel())
    assert fake.comments[0]["body"].endswith("I am the bot now")
    assert len(fake.comments) == 2
