"""Outcome matrix for the CI observability pytest subprocess tests.

Every outcome class the producer must preserve natively: passing, failing,
setup skip, setup xfail, setup error, teardown skip, teardown failure,
multiple failing cleanups, and expected/unexpected xfail results in the
call phase. The strings in the exceptions exist only so native reports
stay distinguishable; the producer records class names, never this text.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def skip_setup() -> None:
    pytest.skip("setup skip on purpose")


@pytest.fixture
def xfail_setup() -> None:
    pytest.xfail("expected failure raised during setup")


@pytest.fixture
def fail_setup() -> None:
    raise RuntimeError("setup failure on purpose")


def test_passes():
    assert True


def test_fails():
    assert 1 == 2


def test_setup_skip(skip_setup: None) -> None:
    raise AssertionError("body must not run after a setup skip")


def test_setup_xfail(xfail_setup: None) -> None:
    raise AssertionError("body must not run after a setup xfail")


def test_setup_fail(fail_setup: None) -> None:
    raise AssertionError("body must not run after a setup error")


def test_teardown_skip(request: pytest.FixtureRequest) -> None:
    def finalize() -> None:
        pytest.skip("teardown skip on purpose")

    request.addfinalizer(finalize)


def test_teardown_fail(request: pytest.FixtureRequest) -> None:
    def finalize() -> None:
        raise RuntimeError("teardown failure on purpose")

    request.addfinalizer(finalize)


def test_multiple_cleanup_failures(request: pytest.FixtureRequest) -> None:
    def first() -> None:
        raise ValueError("cleanup failure one")

    def second() -> None:
        raise KeyError("cleanup failure two")

    request.addfinalizer(first)
    request.addfinalizer(second)


@pytest.mark.xfail
def test_xfail_call():
    raise AssertionError("expected failure in the call phase")


@pytest.mark.xfail
def test_xpass_call():
    assert True
