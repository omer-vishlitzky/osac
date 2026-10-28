"""Strict and conditional xfail classification scenarios.

A strict expected failure that unexpectedly passes is reported by pytest
as a plain failure without ``wasxfail``; the evaluated xfail state on the
item is what distinguishes it from an ordinary failure. A condition that
does not hold, and ``--runxfail``, must leave the native outcome alone.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True)
def test_strict_xpass() -> None:
    assert True


@pytest.mark.xfail(strict=True, condition="False", reason="condition does not hold")
def test_strict_xpass_condition_not_applied() -> None:
    assert True


@pytest.mark.xfail(reason="non-strict", strict=False)
def test_non_strict_xpass() -> None:
    assert True


@pytest.mark.xfail(strict=True, raises=KeyError)
def test_strict_xfail_wrong_exception() -> None:
    raise ValueError("expected failures must stay failures when the exception does not match")


def test_plain_pass() -> None:
    assert True
