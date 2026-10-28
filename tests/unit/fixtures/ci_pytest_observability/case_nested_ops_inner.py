"""Operation affinity, inner level: the outer handle must stay inert here.

The outer module is already imported in-process (under its stem), so
``outer_operation`` is the live handle started under the outer
invocation. This run has its own journal; progress and finish on that
handle must emit nothing here and must not consume its eventual finish.
"""

from __future__ import annotations

import case_nested_ops_outer


def test_inner_cannot_use_outer_operation() -> None:
    assert case_nested_ops_outer.outer_operation is not None
    case_nested_ops_outer.outer_operation.progress(observed_state="inner context")
    case_nested_ops_outer.outer_operation.finish(outcome="passed")
