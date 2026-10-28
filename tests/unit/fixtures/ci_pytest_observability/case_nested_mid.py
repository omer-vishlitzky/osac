"""Three-level nesting, mid-level launcher.

Runs inside a recorded invocation (its own tracker) and spawns an
innermost run that loads the plugin without producer configuration:
that innermost run stays unrecorded and leaves an explicit gap in this
level's journal.
"""

from __future__ import annotations

import pytest


def test_mid_level() -> None:
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
