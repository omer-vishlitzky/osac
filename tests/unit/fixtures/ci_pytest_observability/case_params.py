"""Parameter privacy scenario: raw parameter values must never be recorded.

Both default pytest ids (which embed the value verbatim) and custom ids
built from the value leak the raw parameter into node ids; the producer
must replace the parameter component with a safe hash everywhere it
records an id. The native JUnit and console keep the raw ids.
"""

from __future__ import annotations

import pytest

from tests.e2e.core.observability.context import OperationContext, current_test_ref

CANARY = "SECRETTOKEN-canary"


@pytest.fixture
def parameter_resource() -> str:
    return "resource"


@pytest.mark.parametrize("value", [CANARY, "plain-value"])
def test_param_default_ids(value: str, parameter_resource: str) -> None:
    assert isinstance(value, str)
    assert parameter_resource


@pytest.mark.parametrize("value", [CANARY], ids=lambda v: "ID-" + v)
def test_param_custom_ids(value: str) -> None:
    assert isinstance(value, str)


@pytest.mark.parametrize("value", [CANARY])
def test_operation_under_parametrized(value: str) -> None:
    with OperationContext(name="wait_for_param", expected_condition="done", test=current_test_ref()) as operation:
        operation.progress(observed_state="working")
