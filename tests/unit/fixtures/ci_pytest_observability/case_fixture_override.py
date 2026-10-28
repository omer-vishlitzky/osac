"""Fixture definition override scenarios.

A module-scoped fixture and a class-scoped fixture share one argname in
one module: two distinct definitions that must keep distinct identities.
The module fixture is cached across its users (one setup lifetime), the
class fixture is set up once inside its class, and the finalizer order
log lets the harness compare plugin-on and plugin-off runs.
"""

from __future__ import annotations

import os

import pytest

ORDER_FILE = os.environ.get("CI_OBS_ORDER_FILE")


def _record(event: str) -> None:
    if ORDER_FILE:
        with open(ORDER_FILE, "a", encoding="utf-8") as handle:
            handle.write(event + "\n")


@pytest.fixture(scope="module")
def shared_name() -> str:
    _record("module-shared-name-setup")
    return "module"


@pytest.fixture(scope="class")
def shared_name_class() -> str:
    _record("class-shared-name-class-setup")
    return "class"


class TestOverrides:
    @pytest.fixture(scope="class")
    def shared_name(self) -> str:
        _record("class-shared-name-setup")
        return "class"

    def test_uses_class_override(self, shared_name: str) -> None:
        assert shared_name == "class"

    def test_uses_class_override_again(self, shared_name: str) -> None:
        assert shared_name == "class"

    def test_uses_module_fixture(self, shared_name_class: str) -> None:
        assert shared_name_class == "class"


def test_uses_module_definition(shared_name: str) -> None:
    assert shared_name == "module"


def test_uses_module_definition_again(shared_name: str) -> None:
    assert shared_name == "module"
