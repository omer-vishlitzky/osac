"""Fail-fast scenario: the first test fails and -x stops the run."""

from __future__ import annotations


def test_first_failure():
    # Exercise pytest's assertion rewriting rather than raising directly.
    assert False  # noqa: B011


def test_second():
    assert True


def test_third():
    assert True
