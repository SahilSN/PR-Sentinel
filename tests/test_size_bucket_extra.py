"""Extra boundary tests for the triage size buckets."""

from pr_agent.triage import size_bucket


def test_tiny_change_is_small():
    assert size_bucket(5) == "S"


def test_bucket_boundaries():
    assert size_bucket(10) == "XS"
    assert size_bucket(11) == "S"
    assert size_bucket(501) == "XL"
