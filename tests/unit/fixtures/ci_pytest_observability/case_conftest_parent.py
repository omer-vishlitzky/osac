"""Nested-conftest override scenario, parent definition.

The parent conftest of this fixture tree (this directory's ``conftest.py``)
and the ``nested_pkg`` child conftest both define a module-scoped
``shared_fixture``: same argname, same scope, both functions loaded as
module ``conftest`` with qualname ``shared_fixture``. These tests
consume the parent definition, cached across both tests.
"""

from __future__ import annotations


def test_parent_first(shared_fixture: str) -> None:
    assert shared_fixture == "parent"


def test_parent_second(shared_fixture: str) -> None:
    assert shared_fixture == "parent"
