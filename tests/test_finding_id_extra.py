"""Extra checks for deterministic finding ids."""

from pr_agent.schemas import finding_id


def test_kind_is_slugified():
    assert finding_id("app/a.py", "Null Deref!") == "app/a.py:null-deref"


def test_empty_kind_falls_back():
    assert finding_id("app/a.py", "") == "app/a.py:"
