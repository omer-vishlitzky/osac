"""Nested-conftest override scenario, child definition.

The parent conftest of this fixture tree and this file's conftest both
define a module-scoped ``shared_fixture``: two definitions whose
functions share module ``conftest`` and qualname ``shared_fixture``.
Only the fixture's root-relative namespace (its ``FixtureDef.baseid``)
separates them, which the producer must reflect as two distinct
definition and instance identities. Both tests consume the child
definition, which is cached: one setup lifetime, two use edges.
"""

from __future__ import annotations


def test_child_first(shared_fixture: str) -> None:
    assert shared_fixture == "child"


def test_child_second(shared_fixture: str) -> None:
    assert shared_fixture == "child"
