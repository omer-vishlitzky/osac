"""Operation-context scenario exercising the W08-facing API in a real run.

Uses the typed OperationContext API exactly as a shared wait helper
would: a resource-linked operation under the running test, progress
records, a parent/child dependency, a timeout finish, and an exception
path through the context manager.
"""

from __future__ import annotations

import time

import pytest

from tests.e2e.core.observability.context import OperationContext, ResourceRef, current_test_ref


def test_operation_lifecycle():
    with OperationContext(
        name="wait_for_provision",
        expected_condition="ComputeInstance phase Ready",
        timeout_ms=5000,
        resource=ResourceRef(kind="ComputeInstance", uid="cr-selftest-1", api_id="api-selftest-1"),
        test=current_test_ref(),
    ) as operation:
        time.sleep(0.05)
        operation.progress(observed_state="Provisioning", observed_count=1)

    parent = OperationContext(name="wait_for_cluster", expected_condition="cluster ready")
    parent.start()
    child = OperationContext(name="wait_for_node", expected_condition="node ready", parent=parent)
    child.start()
    child.progress(observed_count=2)
    child.finish(outcome="passed")
    parent.finish(outcome="passed")


def test_operation_timeout():
    operation = OperationContext(name="wait_for_deletion", expected_condition="resource gone")
    operation.start()
    operation.finish(outcome="timeout", failure_signature="DeadlineExceeded")


def test_operation_exception():
    with (
        pytest.raises(ValueError, match="helper failed"),
        OperationContext(name="failing_helper", expected_condition="never reached"),
    ):
        raise ValueError("helper failed")
