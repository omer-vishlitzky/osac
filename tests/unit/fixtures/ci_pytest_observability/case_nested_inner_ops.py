"""Operation scoping, inner level: an outer operation is not a valid parent.

The outer case module is already imported in-process by the nested run
(under its stem, the way pytest imports files from a directory without
``__init__.py``), so ``case_nested_outer_ops.outer_operation`` is the
live object started under the outer invocation; using it as a parent
here must raise, because this run's invocation has its own operation
registry.
"""

from __future__ import annotations

import case_nested_outer_ops
import pytest

from tests.e2e.core.observability.context import OperationContext


def test_inner_cannot_use_outer_parent() -> None:
    assert case_nested_outer_ops.outer_operation is not None
    with pytest.raises(ValueError, match="parent"):
        OperationContext(
            name="inner_wait", expected_condition="inner done", parent=case_nested_outer_ops.outer_operation
        ).start()
