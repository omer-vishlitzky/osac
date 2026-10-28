"""Conditional hooks for producer regressions, inert without opt-in env.

The repository's own suite collects no files from this directory, so
these hooks only run in harness child runs that ask for them:

- ``CI_OBS_ADD_ITEMS=1``: a collection hook appends one item, because
  the producer's collection wrapper must count items another hook adds.
- ``CI_OBS_EMPTY_WORKERINPUT=1``: a tryfirst configure hook sets
  ``config.workerinput = {}``, simulating a real worker whose worker
  input lacks a usable worker id. The producer must never record a
  fabricated serial-main identity for it.
- ``CI_OBS_REPORT_DELAY_MS``: a trylast makereport hook sleeps that many
  milliseconds after each report and appends to the marker file named by
  ``CI_OBS_REPORT_DELAY_MARKER``, proving report-generation latency that
  the producer must not charge to the phase that finished.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest


def _added_body() -> None:
    assert True


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("CI_OBS_ADD_ITEMS") != "1" or not items:
        return
    if any(item.name == "test_added_dynamically" for item in items):
        return
    added = pytest.Function.from_parent(items[0].parent, name="test_added_dynamically", callobj=_added_body)
    items.append(added)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("CI_OBS_EMPTY_WORKERINPUT") == "1":
        config.workerinput = {}


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]) -> Iterator[object]:
    delay = os.environ.get("CI_OBS_REPORT_DELAY_MS")
    marker = os.environ.get("CI_OBS_REPORT_DELAY_MARKER")
    if not delay or not marker:
        yield
        return
    yield
    time.sleep(int(delay) / 1000)
    with open(marker, "a", encoding="utf-8") as handle:
        handle.write(f"{item.nodeid} {call.when}\n")
