"""One passing test, used as the target of nested in-process runs."""

from __future__ import annotations


def test_inner_passes() -> None:
    assert True
