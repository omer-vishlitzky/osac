"""Fixture lifecycle matrix for the CI observability pytest subprocess tests.

Covers session/module/function scopes, parameterized fixture instances
with cache transitions, a fixture factory that registers finalizers
during the test body, and dependent fixtures whose teardown order must
stay exactly pytest's own. The order log goes to the file named by
CI_OBS_ORDER_FILE so the plugin-on and plugin-off runs can be compared
byte for byte.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator

import pytest

ORDER_FILE = os.environ.get("CI_OBS_ORDER_FILE")


def _record(event: str) -> None:
    if ORDER_FILE:
        with open(ORDER_FILE, "a", encoding="utf-8") as handle:
            handle.write(event + "\n")


@pytest.fixture(scope="session")
def session_resource() -> Iterator[str]:
    _record("session-setup")
    yield "session"
    _record("session-teardown")


@pytest.fixture(scope="module")
def module_resource() -> Iterator[str]:
    _record("module-setup")
    time.sleep(0.05)
    yield "module"
    time.sleep(0.05)
    _record("module-teardown")


@pytest.fixture
def function_resource() -> Iterator[str]:
    _record("function-setup")
    yield "function"
    _record("function-teardown")


@pytest.fixture(scope="module", params=["alpha", "beta"])
def parameterized_resource(request: pytest.FixtureRequest) -> Iterator[str]:
    _record(f"param-setup-{request.param}")
    time.sleep(0.05)
    yield request.param
    time.sleep(0.05)
    _record(f"param-teardown-{request.param}")


@pytest.fixture
def late_finalizer_factory(request: pytest.FixtureRequest) -> Callable[[str, float], None]:
    """Fixture factory registering finalizers DURING the test body."""

    def register(name: str, delay: float) -> None:
        def finalizer() -> None:
            time.sleep(delay)
            _record(f"late-{name}")

        request.addfinalizer(finalizer)

    return register


@pytest.fixture
def dependency_resource() -> Iterator[str]:
    _record("dependency-setup")
    yield "dependency"
    _record("dependency-teardown")


@pytest.fixture
def dependent_resource(dependency_resource: str) -> Iterator[str]:
    _record("dependent-setup")
    yield "dependent"
    _record("dependent-teardown")


def test_uses_shared(session_resource: str, module_resource: str, function_resource: str) -> None:
    assert session_resource and module_resource and function_resource


def test_idle_gap(module_resource: str) -> None:
    # A deliberate idle span while the module fixture is alive: its cost
    # must stay out of both the setup and the teardown measurement.
    time.sleep(0.3)
    assert module_resource


def test_param_first(parameterized_resource: str) -> None:
    assert parameterized_resource in ("alpha", "beta")


def test_param_second(parameterized_resource: str) -> None:
    assert parameterized_resource in ("alpha", "beta")


def test_late_finalizers(late_finalizer_factory: Callable[[str, float], None]) -> None:
    late_finalizer_factory("one", delay=0.2)
    late_finalizer_factory("two", delay=0.05)


def test_dependent_fixtures(dependent_resource: str) -> None:
    assert dependent_resource
