"""Direct-parametrization lifetime identity scenario.

pytest delivers ``@pytest.mark.parametrize`` values through a fresh
per-mark fixture (module ``_pytest.python``, qualname
``get_direct_param_fixture_func``, root baseid), so two tests that
parametrize the same argument name at the same parameter index share a
definition identity while being separate setup-to-teardown lifetimes.
The two ``crossed`` tests swap their value order, so index 0 of one and
index 0 of the other hold different values under the same identity; the
``same_value`` test repeats a value another mark also uses. A cached
user fixture (``shared_user_resource``) served two tests must stay one
lifetime.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def shared_user_resource() -> str:
    return "shared"


@pytest.mark.parametrize("value", ["first-zero", "first-one"])
def test_direct_param_crossed_first(value: str, shared_user_resource: str) -> None:
    assert isinstance(value, str)
    assert shared_user_resource


@pytest.mark.parametrize("value", ["second-zero", "second-one"])
def test_direct_param_crossed_second(value: str, shared_user_resource: str) -> None:
    assert isinstance(value, str)
    assert shared_user_resource


@pytest.mark.parametrize("value", ["first-zero"])
def test_direct_param_same_value(value: str) -> None:
    # The same value another mark parametrizes at the same index: a
    # distinct lifetime that must not merge with that mark's either.
    assert isinstance(value, str)
