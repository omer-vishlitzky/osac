"""Collection-error scenario: the module import fails on purpose."""

from __future__ import annotations


def test_never_collected():
    assert True


import ci_observability_missing_module_on_purpose  # noqa: E402, F401
