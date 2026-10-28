"""repr-budget scenario: parameter values whose repr counts its calls.

The assertions fail on purpose so pytest's own failure formatting calls
repr on the parameter values in both the plugin-on and plugin-off runs;
the producer must not add a single extra call. The counts go to the file
named by CI_OBS_REPR_FILE.
"""

from __future__ import annotations

import os

import pytest

REPR_FILE = os.environ.get("CI_OBS_REPR_FILE")


class CountingParam:
    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        if REPR_FILE:
            with open(REPR_FILE, "a", encoding="utf-8") as handle:
                handle.write("1\n")
        return f"CountingParam({self.label})"


@pytest.fixture(scope="module", params=[CountingParam("a"), CountingParam("b")])
def counting(request: pytest.FixtureRequest):
    return request.param


def test_counting_first(counting: CountingParam):
    assert counting.label == "never", "fails on purpose so pytest formats the value"


def test_counting_second(counting: CountingParam):
    assert counting.label == "never", "fails on purpose so pytest formats the value"
