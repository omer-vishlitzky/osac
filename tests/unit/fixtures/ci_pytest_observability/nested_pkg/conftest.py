"""The child definition of the nested-conftest override scenario."""

from __future__ import annotations

import os

import pytest

ORDER_FILE = os.environ.get("CI_OBS_ORDER_FILE")


@pytest.fixture(scope="module")
def shared_fixture() -> str:
    if ORDER_FILE:
        with open(ORDER_FILE, "a", encoding="utf-8") as handle:
            handle.write("child-conftest-shared-fixture-setup\n")
    return "child"
