"""Two-level nesting: an inner pytest.main that loads the plugin but has
no output/context of its own.

The inner run must stay unrecorded with an explicit gap in the outer
journal, and the outer tracker must survive it: the test that runs after
the nested call is still recorded, the outer invocation is finished once
with the outer exit status, and the outer bundle stays contract-valid.
"""

from __future__ import annotations

import pytest


def test_spawns_unconfigured_inner_run() -> None:
    rc = pytest.main(
        [
            "-o",
            "addopts=",
            "-p",
            "tests.e2e.core.observability.plugin",
            "tests/unit/fixtures/ci_pytest_observability/case_inner.py",
        ]
    )
    assert rc == 0


def test_after_nested_run() -> None:
    assert True
