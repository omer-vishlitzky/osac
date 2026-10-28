"""Operation scoping, outer level: starts an operation, then runs a
configured nested pytest run whose test tries to use it as a parent.

The inner run must reject the outer operation as a parent (per-invocation
operation registry), and after the nested run returns the outer registry
must be restored so the outer parent-child pair still works.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.core.observability.context import OperationContext

outer_operation: OperationContext | None = None


def test_outer_starts_operation() -> None:
    global outer_operation
    outer_operation = OperationContext(
        name="outer_wait", expected_condition="outer done", operation_id="op-outer-fixed"
    )
    outer_operation.start()


def test_outer_runs_nested() -> None:
    output = os.environ["CI_OBS_OPS_OUTPUT"]
    context = os.environ["CI_OBS_OPS_CONTEXT"]
    rc = pytest.main(
        [
            "-o",
            "addopts=",
            "-p",
            "tests.e2e.core.observability.plugin",
            f"--ci-observability-output={output}",
            f"--ci-observability-context={context}",
            "tests/unit/fixtures/ci_pytest_observability/case_nested_inner_ops.py",
        ]
    )
    assert rc == 0


def test_outer_parent_child_after_nested() -> None:
    assert outer_operation is not None
    child = OperationContext(name="outer_child", expected_condition="child done", parent=outer_operation)
    child.start()
    child.finish(outcome="passed")
    outer_operation.finish(outcome="passed")
