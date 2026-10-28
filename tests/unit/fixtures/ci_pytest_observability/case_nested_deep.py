"""Three-level nesting, outermost launcher.

The mid-level run (case_nested_mid.py) is fully configured and records
itself as its own invocation; the innermost run inside it is
unconfigured and stays unrecorded with a gap. Both the outer and the
mid-level trackers must finish correctly, proving the per-config
suspend map restores the right tracker at every level.
"""

from __future__ import annotations

import os

import pytest


def test_three_level_nesting() -> None:
    output = os.environ["CI_OBS_DEEP_OUTPUT"]
    context = os.environ["CI_OBS_DEEP_CONTEXT"]
    rc = pytest.main(
        [
            "-o",
            "addopts=",
            "-p",
            "tests.e2e.core.observability.plugin",
            f"--ci-observability-output={output}",
            f"--ci-observability-context={context}",
            "tests/unit/fixtures/ci_pytest_observability/case_nested_mid.py",
        ]
    )
    assert rc == 0


def test_after_deep_nesting() -> None:
    assert True
