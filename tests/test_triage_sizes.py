"""Size buckets used for PR Sentinel's size labels."""

from pr_agent.triage import size_bucket


def test_small_changes():
    assert size_bucket(3) == "XS"
    assert size_bucket(42) == "S"


def test_large_changes():
    assert size_bucket(250) == "M"
    assert size_bucket(900) == "XL"
