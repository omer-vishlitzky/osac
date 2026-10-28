"""Selector privacy scenarios: -k and -m expressions quoting a canary.

The raw filter text must never reach a producer file; only its safe hash,
with membership and counts preserved. The canary marker name is applied
through getattr because a dash is not valid attribute syntax.
"""

from __future__ import annotations

import pytest

canary_mark = getattr(pytest.mark, "EN-canary")


@canary_mark
def test_canary_marked() -> None:
    assert True


def test_not_marked() -> None:
    assert True


def test_canary_named() -> None:
    assert True
