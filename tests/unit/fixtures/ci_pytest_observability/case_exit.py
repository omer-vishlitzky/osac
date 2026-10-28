"""Worker-loss scenario: one test kills its own process with os._exit.

The kill happens only when CI_OBS_KILL_WORKER=1, so the same file also
runs normally for the plugin-off comparison.
"""

from __future__ import annotations

import os
import sys


def test_worker_victim():
    if os.environ.get("CI_OBS_KILL_WORKER") == "1":
        sys.stdout.flush()
        os._exit(9)


def test_survivor():
    assert True
