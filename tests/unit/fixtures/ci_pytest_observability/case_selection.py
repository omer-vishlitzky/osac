"""Marker-based selection scenario: one marked test, one unmarked."""

from __future__ import annotations

import pytest


@pytest.mark.ci_obs_fast
def test_fast():
    assert True


def test_slow():
    assert True
