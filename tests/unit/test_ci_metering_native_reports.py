"""Keep native reports from restored metering suites without changing exits.

Reports stay private. Assertions check the suite catalog and outcomes without
printing native report content.

Run from the repository root:

    uv run pytest -n 0 tests/unit/test_ci_metering_native_reports.py
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from test_ci_native_reports import (  # type: ignore[attr-defined]
    GINKGO_BIN,
    GINKGO_REPORT_EVIDENCE_ERROR,
    REPO_ROOT,
    _env_with_fake_runners,
    _ginkgo_finals,
    _read_ginkgo_merged,
    _read_status,
    _single_attempt_dir,
    _write_fake_runner,
    run_helper,
)
from test_ci_native_reports import stable_dir as stable_dir

requires_ginkgo = pytest.mark.skipif(GINKGO_BIN is None, reason="ginkgo CLI not available")

METERING_SERVICE = REPO_ROOT / "osac-metering" / "metering-service"
# The six Ginkgo suites under metering-service/internal. The plain Go test
# in internal/database is not a Ginkgo suite and publishes no final.
SIX_SUITE_CATALOG = [
    "Events Suite",
    "Heartbeat Suite",
    "Kafka Suite",
    "Projection Suite",
    "Reconciliation Suite",
    "Watch Suite",
]


@requires_ginkgo
def test_all_six_metering_service_suites_publish_valid_native_finals(*, stable_dir: Path) -> None:
    manifest = stable_dir / "expected-suites.txt"
    manifest.write_text("\n".join(SIX_SUITE_CATALOG) + "\n", encoding="utf-8")
    output_dir = stable_dir / "reports"

    process = run_helper(
        "ginkgo",
        output_dir,
        [GINKGO_BIN, "run", "-r", "internal"],
        cwd=METERING_SERVICE,
        expected_suites=manifest,
        timeout=900,
    )

    assert process.returncode == 0, process.stderr
    attempt = _single_attempt_dir(output_dir)
    finals = _ginkgo_finals(attempt)
    assert len(finals) == len(SIX_SUITE_CATALOG)
    merged = _read_ginkgo_merged(attempt)
    assert sorted(suite["SuiteDescription"] for suite in merged) == sorted(SIX_SUITE_CATALOG)
    assert all(suite["SuiteSucceeded"] is True for suite in merged)
    status = _read_status(attempt)
    assert status["state"] == "finished"
    assert status["errors"] == []
    assert status["coverage"]["state"] == "complete"


@requires_ginkgo
def test_restored_events_suite_stays_green_when_collection_is_denied(*, stable_dir: Path) -> None:
    """Use a fast pure suite so output-denial checks need no database."""
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    sabotage = 'chmod 500 "$OSAC_CI_GINKGO_REPORT_DIR"'
    _write_fake_runner(fake_bin, "ginkgo", f'{sabotage}\nexec {shlex.quote(GINKGO_BIN)} "$@"')
    output_dir = stable_dir / "reports"

    process = run_helper(
        "ginkgo",
        output_dir,
        ["ginkgo", "run", "./internal/events"],
        cwd=METERING_SERVICE,
        env=_env_with_fake_runners(fake_bin),
        timeout=600,
    )

    assert process.returncode == 0
    assert GINKGO_REPORT_EVIDENCE_ERROR.encode() in process.stderr
    attempt = _single_attempt_dir(output_dir)
    assert _ginkgo_finals(attempt) == []
    assert _read_status(attempt)["state"] == "running"
