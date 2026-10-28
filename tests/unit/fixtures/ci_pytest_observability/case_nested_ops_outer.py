"""Operation affinity, outer level: an operation started in the outer
invocation survives a configured nested run that tries to use it.

The nested run's test calls progress and finish on the outer handle:
those calls must emit nothing into the inner invocation and must not
consume the finish, so the outer invocation can still finish the
operation correctly after the nested run returns.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.core.observability.context import OperationContext

outer_operation: OperationContext | None = None


def test_outer_starts_operation() -> None:
    global outer_operation
    outer_operation = OperationContext(
        name="outer_affinity_wait", expected_condition="outer done", operation_id="op-affinity-outer"
    )
    outer_operation.start()


def test_outer_runs_nested() -> None:
    output = os.environ["CI_OBS_AFFINITY_OUTPUT"]
    context = os.environ["CI_OBS_AFFINITY_CONTEXT"]
    rc = pytest.main(
        [
            "-o",
            "addopts=",
            "-p",
            "tests.e2e.core.observability.plugin",
            f"--ci-observability-output={output}",
            f"--ci-observability-context={context}",
            "tests/unit/fixtures/ci_pytest_observability/case_nested_ops_inner.py",
        ]
    )
    assert rc == 0


def test_outer_finishes_after_nested() -> None:
    assert outer_operation is not None
    outer_operation.progress(observed_state="restored")
    outer_operation.finish(outcome="passed")
