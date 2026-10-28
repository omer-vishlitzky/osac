"""Interrupted-teardown scenario for the SIGINT subprocess test.

The body signals it is done by writing the path in CI_OBS_BODY_DONE; the
finalizer then sleeps long enough for the harness to deliver SIGINT
during the teardown phase.
"""

from __future__ import annotations

import os
import time

import pytest


def test_interrupted_teardown(request: pytest.FixtureRequest):
    marker = os.environ.get("CI_OBS_BODY_DONE")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("done")

    def slow_finalizer() -> None:
        time.sleep(60)

    request.addfinalizer(slow_finalizer)
