"""Real-subprocess tests for the CI observability pytest producer (W02).

Every scenario runs a real pytest child process (serial or under
pytest-xdist) through the explicit opt-in plugin under
``tests/e2e/core/observability/``, wraps it in one job header/footer from
the bootstrap helper, and validates the produced bundle with the frozen
contract validators imported from the pinned osac-test-infra checkout.
Native behavior is compared against plugin-off runs: exit codes, JUnit
outcomes, and finalizer order must be identical.

Environment:

- ``OSAC_CI_OBS_CONTRACT_DIR``: the ``monitoring/ci_observability/contracts``
  directory of the pinned osac-test-infra checkout (commit c2cbeb5).
  Required for the oracle; when set but unusable these tests fail, and
  when unset they skip with an explicit reason.
- ``OSAC_CI_OBS_PYTEST_PYTHON``: optional second child interpreter (for
  example the repository venv with pytest 9.1.1 and pytest-xdist). The
  test session's own interpreter is always used as the first child.

Suggested invocation (the test interpreter must provide jsonschema and
referencing for the oracle):

    OSAC_CI_OBS_CONTRACT_DIR=<infra>/monitoring/ci_observability/contracts \\
    OSAC_CI_OBS_PYTEST_PYTHON=<repo>/.venv/bin/python \\
    <python-with-jsonschema> -m pytest -o addopts="" \\
        tests/unit/test_ci_pytest_observability.py
"""

from __future__ import annotations

import ast
import contextlib
import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.core.observability import context as observability_context
from tests.e2e.core.observability.context import ContextError, ExecutionClaims, OperationContext, ResourceRef, TestRef
from tests.e2e.core.observability.plugin import Tracker, _filter_identity
from tests.e2e.core.observability.writer import (
    JobJournal,
    JournalWriter,
    RunDirs,
    safe_test_id,
    write_collection_sidecar,
    write_job_header,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = "tests/unit/fixtures/ci_pytest_observability"
CONTRACT_ENV = "OSAC_CI_OBS_CONTRACT_DIR"
ALT_PYTHON_ENV = "OSAC_CI_OBS_PYTEST_PYTHON"
CANARY_SECRET = "ci-obs-canary-secret-value-3f9a"
PLUGIN = "tests.e2e.core.observability.plugin"

_MODULE_CACHE: dict[tuple[str, str], bool] = {}


@pytest.fixture(scope="module")
def oracle() -> tuple[object, object]:
    """The frozen contract validators, imported from the pinned checkout."""
    raw = os.environ.get(CONTRACT_ENV)
    if not raw:
        pytest.skip(f"{CONTRACT_ENV} is not set: frozen-contract oracle unavailable")
    contract_dir = Path(raw).resolve()
    repo_root = contract_dir.parents[2]
    if not (repo_root / "monitoring" / "ci_observability" / "contracts" / "validation.py").is_file():
        pytest.fail(f"{CONTRACT_ENV} does not point at the frozen contract checkout: {raw}")
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from monitoring.ci_observability.contracts import derivation, validation
    except ModuleNotFoundError as error:
        pytest.fail(
            f"contract oracle dependencies missing in this interpreter: {error.name}; "
            "run the module with the interpreter that provides jsonschema and referencing"
        )
    return validation, derivation


def _validate_bundle(*, oracle: tuple[object, object], bundle: Path) -> None:
    validation, _derivation = oracle
    validation.validate_bundle(bundle_dir=bundle)


def _resolve_child(*, kind: str) -> str:
    if kind == "session":
        return sys.executable
    alt = os.environ.get(ALT_PYTHON_ENV)
    if not alt:
        pytest.skip(f"{ALT_PYTHON_ENV} is not set: alternate child interpreter unavailable")
    if not (Path(alt).is_file() and os.access(alt, os.X_OK)):
        pytest.fail(f"{ALT_PYTHON_ENV} is not an executable: {alt}")
    return alt


def _has_module(*, python: str, module: str) -> bool:
    key = (python, module)
    if key not in _MODULE_CACHE:
        try:
            subprocess.run([python, "-c", f"import {module}"], capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            _MODULE_CACHE[key] = False
        else:
            _MODULE_CACHE[key] = True
    return _MODULE_CACHE[key]


def _xdist_child() -> str:
    for kind in ("session", "alt"):
        with contextlib.suppress(pytest.skip.Exception):
            python = _resolve_child(kind=kind)
            if _has_module(python=python, module="xdist"):
                return python
    pytest.fail(f"no child interpreter with pytest-xdist: set {ALT_PYTHON_ENV} to a pytest-xdist interpreter")


def _repo_deps_child() -> str:
    for kind in ("alt", "session"):
        with contextlib.suppress(pytest.skip.Exception):
            python = _resolve_child(kind=kind)
            if _has_module(python=python, module="yaml") and _has_module(python=python, module="websocket"):
                return python
    pytest.skip("no child interpreter with the repository test dependencies (yaml, websocket-client)")


@pytest.fixture
def work_dir() -> Iterator[Path]:
    """A temp directory outside pytest's managed tree, for subprocess output.

    Children write their run roots and reports here for the duration of
    one test; pytest's numbered-directory cleanup has deleted running
    subprocess trees under the full unit suite before. Cleanup failures
    are not hidden: permissions are restored only inside this owned
    scratch path, never following a link out of it, and the retry raises
    so no debris leaks silently.
    """
    directory = Path(tempfile.mkdtemp(prefix="ci-pytest-observability-", dir=REPO_ROOT.parent))
    yield directory
    try:
        shutil.rmtree(directory)
    except OSError:
        _restore_owned_permissions(root=directory)
        shutil.rmtree(directory)


def _restore_owned_permissions(*, root: Path) -> None:
    """Make every directory under an owned scratch path writable again."""
    for path in [root, *root.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            with contextlib.suppress(OSError):
                path.chmod(0o700)


def _case(*, name: str) -> str:
    return f"{FIXTURES}/{name}"


def _git_sha(*, directory: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha if len(sha) == 40 else None


def _workflow_sha() -> str | None:
    contract_dir = os.environ.get(CONTRACT_ENV)
    if contract_dir:
        repo = Path(contract_dir).resolve().parents[2]
        if repo.is_dir():
            return _git_sha(directory=repo)
    return None


def _claims_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "execution_id": f"exec-{uuid.uuid4().hex[:16]}",
        "repository_id": 987654,
        "repository": "osac-project/osac",
        "workflow": {"path": ".github/workflows/tests.yml", "revision": _workflow_sha()},
        "run_id": 123456789,
        "run_attempt": 1,
        "event": "push",
        "cohort": "pull-request",
        "job": {
            "logical_job_key": "tests.yml#ci-observability-selftest",
            "github_job_id": None,
            "job_name": "ci-observability-selftest",
        },
        "suite": {"framework": "pytest", "tier": "unit", "component": "e2e", "variant": "ci-observability-selftest"},
        "sources": [
            {"role": "tested", "repository": "osac-project/osac", "revision": _git_sha(directory=REPO_ROOT)},
            {"role": "workflow", "repository": "osac-project/osac-test-infra", "revision": _workflow_sha()},
        ],
    }
    payload.update(overrides)
    return payload


def _child_env(*, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child environment without producer variables, plus overrides.

    Stripping OSAC_CI_OBS_* keeps plugin-off comparison runs honest even
    when the test session itself was started with producer variables.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("OSAC_CI_OBS_")}
    env.update(extra or {})
    return env


@dataclass
class ChildRun:
    returncode: int
    stdout: str
    stderr: str
    junit: Path | None


@dataclass
class JobHarness:
    """One job execution: one header, one or more child runs, one footer."""

    root: Path
    bundle: Path
    context_path: Path
    header: JobJournal
    exit_codes: list[int] = field(default_factory=list)
    junit_counter: int = 0
    junit_dir: Path = field(default_factory=Path)

    @classmethod
    def create(cls, *, directory: Path, claims: dict[str, object] | None = None) -> JobHarness:
        directory.mkdir(parents=True, exist_ok=True)
        root = directory / "run"
        context_path = directory / "context.json"
        payload = claims if claims is not None else _claims_payload()
        context_path.write_text(json.dumps(payload), encoding="utf-8")
        parsed = ExecutionClaims.from_mapping(payload=payload)
        header = write_job_header(
            execution=parsed.execution_payload(),
            suite=parsed.suite_payload(),
            environment=parsed.environment_payload(),
            run_root=root,
        )
        junit_dir = directory / "junit"
        junit_dir.mkdir(parents=True, exist_ok=True)
        return cls(root=root, bundle=root / "bundle", context_path=context_path, header=header, junit_dir=junit_dir)

    def next_junit(self) -> Path:
        self.junit_counter += 1
        return self.junit_dir / f"junit-{self.junit_counter}.xml"

    def run(
        self,
        *,
        python: str,
        args: Sequence[str],
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        junit: bool = True,
        plugin: bool = True,
        keep_addopts: bool = False,
        timeout: float = 300.0,
    ) -> ChildRun:
        command = [python, "-m", "pytest"]
        if not keep_addopts:
            command += ["-o", "addopts="]
        if plugin:
            command += [
                "-p",
                PLUGIN,
                f"--ci-observability-output={self.root}",
                f"--ci-observability-context={self.context_path}",
            ]
        junit_path = self.next_junit() if junit else None
        if junit_path is not None:
            command += [f"--junitxml={junit_path}"]
        command += list(extra_args)
        command += list(args)
        result = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, env=_child_env(extra=env)
        )
        self.exit_codes.append(result.returncode)
        return ChildRun(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr, junit=junit_path)

    def finish(self, *, end_reason: str = "finished", native_reports: Sequence[tuple[Path, str]] = ()) -> Path:
        if any(code == 2 for code in self.exit_codes):
            outcome = "cancelled"
        elif self.exit_codes and all(code == 0 for code in self.exit_codes):
            outcome = "passed"
        elif self.exit_codes:
            outcome = "failed"
        else:
            outcome = "unknown"
        exit_code = self.exit_codes[-1] if self.exit_codes else None
        return self.header.finish(
            outcome=outcome, exit_code=exit_code, end_reason=end_reason, native_reports=native_reports
        )


@dataclass
class BundleView:
    """Parsed view of one produced bundle."""

    bundle: Path
    manifest: dict[str, object]
    journals: dict[str, list[dict[str, object]]]

    def records(self, *, event: str) -> list[dict[str, object]]:
        found = []
        for records in self.journals.values():
            found.extend(record for record in records if record["event"] == event)
        return found

    def invocation_journal(self) -> str:
        for stem, records in self.journals.items():
            if any(record["event"] == "invocation_started" for record in records):
                return stem
        raise AssertionError("no invocation journal found")

    def worker_journals(self) -> list[str]:
        return [
            stem
            for stem, records in self.journals.items()
            if any(record["event"] == "worker_started" for record in records)
        ]

    def phases(self) -> dict[tuple[str, int], dict[str, dict[str, object]]]:
        by_attempt: dict[tuple[str, int], dict[str, dict[str, object]]] = {}
        for records in self.journals.values():
            for record in records:
                if record["event"] != "test_phase_finished":
                    continue
                data = record["data"]
                key = (str(data["test_id"]), int(data["attempt_index"]))
                by_attempt.setdefault(key, {})[str(data["phase"])] = data
        return by_attempt

    def derived(self, *, derivation: object) -> dict[tuple[str, int], object]:
        return {
            key: derivation.derive_attempt_result(phases=list(phase_map.values()))
            for key, phase_map in self.phases().items()
        }

    def fixture_events(self, *, fixture_id_suffix: str) -> list[dict[str, object]]:
        found = []
        for records in self.journals.values():
            for record in records:
                if record["event"] in ("fixture_started", "fixture_finished") and str(
                    record["data"].get("fixture_id", "")
                ).endswith(fixture_id_suffix):
                    found.append(record)
        return found


def _load_bundle(*, bundle: Path) -> BundleView:
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    journals: dict[str, list[dict[str, object]]] = {}
    for path in sorted((bundle / "journals").glob("*.jsonl")):
        journals[path.stem] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return BundleView(bundle=bundle, manifest=manifest, journals=journals)


def _run_validated_job(
    *,
    oracle: tuple[object, object],
    directory: Path,
    python: str,
    case: str,
    extra_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    end_reason: str = "finished",
    keep_addopts: bool = False,
) -> tuple[JobHarness, ChildRun, BundleView]:
    harness = JobHarness.create(directory=directory)
    child = harness.run(python=python, args=[case], extra_args=extra_args, env=env, keep_addopts=keep_addopts)
    native = [(child.junit, "junit.xml")] if child.junit else []
    bundle = harness.finish(end_reason=end_reason, native_reports=native)
    _validate_bundle(oracle=oracle, bundle=bundle)
    return harness, child, _load_bundle(bundle=bundle)


def _junit_cases(*, path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    cases: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        cases[name] = outcome
    return cases


def _assert_native_unchanged(
    *,
    directory: Path,
    python: str,
    args: Sequence[str],
    plugin_run: ChildRun,
    extra_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> None:
    """The same run without the plugin must produce the same native result."""
    junit_path = directory / "junit-off.xml"
    command = [python, "-m", "pytest", "-o", "addopts=", f"--junitxml={junit_path}"]
    command += list(extra_args)
    command += list(args)
    off = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, env=_child_env(extra=env))
    assert off.returncode == plugin_run.returncode, (
        f"exit code changed with the plugin: {off.returncode} != {plugin_run.returncode}"
    )
    if plugin_run.junit is not None:
        assert _junit_cases(path=plugin_run.junit) == _junit_cases(path=junit_path), (
            "native JUnit changed with the plugin"
        )


def _read_lines(*, path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _parse_utc(*, value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_execution_claims_reject_bad_documents() -> None:
    good = _claims_payload()
    ExecutionClaims.from_mapping(payload=good)

    def expect_error(*, payload: dict[str, object], fragment: str) -> None:
        with pytest.raises(ContextError, match=fragment):
            ExecutionClaims.from_mapping(payload=payload)

    missing_key = dict(good)
    del missing_key["run_id"]
    expect_error(payload=missing_key, fragment="context-missing-run-id")

    unknown_key = dict(good)
    unknown_key["extra"] = 1
    expect_error(payload=unknown_key, fragment="context-unknown-key")

    bad_event = dict(good)
    bad_event["event"] = "not-an-event"
    expect_error(payload=bad_event, fragment="context-event-not-allowed")

    duplicate_role = dict(good)
    duplicate_role["sources"] = [
        {"role": "tested", "repository": "a/b", "revision": None},
        {"role": "tested", "repository": "a/b", "revision": None},
    ]
    expect_error(payload=duplicate_role, fragment="context-source-role-duplicate")

    missing_role = dict(good)
    missing_role["sources"] = [{"role": "tested", "repository": "a/b", "revision": None}]
    expect_error(payload=missing_role, fragment="context-source-role-missing-workflow")

    string_job_id = dict(good)
    string_job_id["job"] = {"logical_job_key": "k", "github_job_id": "e2e"}
    expect_error(payload=string_job_id, fragment="context-github-job-id-invalid")

    bad_revision = dict(good)
    bad_revision["workflow"] = {"path": ".github/workflows/tests.yml", "revision": "abc123"}
    expect_error(payload=bad_revision, fragment="context-workflow-revision-invalid")


def test_claims_payloads_round_trip() -> None:
    claims = ExecutionClaims.from_mapping(payload=_claims_payload())
    execution = claims.execution_payload()
    assert execution["job"]["github_job_id"] is None
    assert claims.environment_payload()["sources"][0]["role"] == "tested"
    assert claims.suite_payload()["framework"] == "pytest"


def test_operation_context_api() -> None:
    ref = TestRef(test_id="some.py::test_x", attempt_index=1, worker_id="main")
    assert ref.payload() == {"test_id": "some.py::test_x", "attempt_index": 1, "worker_id": "main"}

    resource = ResourceRef(kind="ComputeInstance", uid="cr-1", api_id="api-1")
    assert resource.payload() == {"kind": "ComputeInstance", "uid": "cr-1", "api_id": "api-1"}

    operation = OperationContext(name="wait_for_x", expected_condition="x ready")
    assert operation.operation_id.startswith("op-")

    with pytest.raises(ValueError, match="outcome"):
        operation.finish(outcome="green")

    with pytest.raises(ValueError, match="name"):
        OperationContext(name="", expected_condition="x")

    # Inert without an active journal: fully inert. No registry is
    # mutated and no state-dependent validation runs, so duplicates and
    # unstarted parents do not raise.
    duplicate_first = OperationContext(name="first", expected_condition="x", operation_id="op-inert")
    duplicate_first.start()
    duplicate_second = OperationContext(name="second", expected_condition="y", operation_id="op-inert")
    duplicate_second.start()
    unstarted = OperationContext(name="unstarted", expected_condition="u")
    OperationContext(name="orphan", expected_condition="o", parent=unstarted).start()
    duplicate_first.finish(outcome="passed")
    duplicate_second.finish(outcome="passed")


def test_operation_context_active_validation(work_dir: Path) -> None:
    """Duplicate ids and unknown parents raise only while a journal is active."""
    writer = JournalWriter(run_dirs=RunDirs(journals=work_dir, state=work_dir), journal_id="ops", execution_id="exec-x")
    observability_context._bind_state(journal=writer, operations=set())
    try:
        first = OperationContext(name="first", expected_condition="x", operation_id="op-fixed")
        first.start()
        with pytest.raises(ValueError, match="already used"):
            OperationContext(name="second", expected_condition="y", operation_id="op-fixed").start()
        unstarted = OperationContext(name="unstarted", expected_condition="u")
        with pytest.raises(ValueError, match="parent"):
            OperationContext(name="orphan", expected_condition="o", parent=unstarted).start()
        child = OperationContext(name="child", expected_condition="c", parent=first)
        child.start()
        child.finish(outcome="passed")
        first.finish(outcome="passed")
    finally:
        observability_context._bind_state(journal=None, operations=None)

    # Unbound again: fully inert, including the previously rejected cases.
    OperationContext(name="again", expected_condition="x", operation_id="op-fixed").start()
    writer.close(status="stopped")
    starts = [
        record
        for record in _journal_records(directory=work_dir, journal_id="ops")
        if record["event"] == "operation_started"
    ]
    assert {record["data"]["operation_id"] for record in starts} == {"op-fixed", child.operation_id}


def test_writer_counts_drops_and_never_raises(work_dir: Path) -> None:
    broken = JournalWriter(
        run_dirs=RunDirs(journals=None, state=None, problems=("journals-dir-unusable-eacces",)),
        journal_id="broken",
        execution_id="exec-x",
    )
    assert not broken.emit(
        event="collection_state", data={"source": "x", "status": "stopped", "complete": True, "dropped_records": 0}
    )
    assert broken.dropped_records == 1
    assert broken.errors[0] == "journals-dir-unusable-eacces"
    assert "journal-unavailable" in broken.errors
    broken.close(status="degraded")

    healthy = JournalWriter(
        run_dirs=RunDirs(journals=work_dir, state=work_dir), journal_id="healthy", execution_id="exec-x"
    )
    assert healthy.emit(
        event="collection_state", data={"source": "x", "status": "stopped", "complete": True, "dropped_records": 0}
    )
    # A payload that cannot serialize is a counted drop, not an exception.
    assert not healthy.emit(event="operation_started", data={"name": object()})
    assert healthy.dropped_records == 1
    healthy.close(status="degraded")
    assert (work_dir / "writer-status-healthy.json").is_file()


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_serial_outcomes_and_phases(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_outcomes.py")
    )
    assert child.returncode == 1
    _assert_native_unchanged(directory=work_dir, python=python, args=[_case(name="case_outcomes.py")], plugin_run=child)

    _validation, derivation = oracle
    outcomes = {key[0]: result.outcome for key, result in view.derived(derivation=derivation).items()}
    assert outcomes == {
        f"{_case(name='case_outcomes.py')}::test_passes": "passed",
        f"{_case(name='case_outcomes.py')}::test_fails": "failed",
        f"{_case(name='case_outcomes.py')}::test_setup_skip": "skipped",
        f"{_case(name='case_outcomes.py')}::test_setup_xfail": "expected_failure",
        f"{_case(name='case_outcomes.py')}::test_setup_fail": "error",
        f"{_case(name='case_outcomes.py')}::test_teardown_skip": "passed",
        f"{_case(name='case_outcomes.py')}::test_teardown_fail": "failed",
        f"{_case(name='case_outcomes.py')}::test_multiple_cleanup_failures": "failed",
        f"{_case(name='case_outcomes.py')}::test_xfail_call": "expected_failure",
        f"{_case(name='case_outcomes.py')}::test_xpass_call": "unexpected_pass",
    }

    phases = view.phases()
    skipped_setup = phases[(_case(name="case_outcomes.py") + "::test_setup_skip", 1)]
    assert skipped_setup["setup"]["outcome"] == "skipped"
    assert "call" not in skipped_setup
    assert skipped_setup["teardown"]["outcome"] == "passed"

    cleanup = phases[(_case(name="case_outcomes.py") + "::test_multiple_cleanup_failures", 1)]
    assert cleanup["teardown"]["outcome"] == "error"
    assert cleanup["teardown"]["failure"]["signature"] == "KeyError|ValueError"

    first_failure = view.records(event="invocation_finished")[0]["data"]["failure"]
    assert first_failure["signature"] == "AssertionError"
    assert first_failure["phase"] == "call"

    started = view.records(event="invocation_started")[0]["data"]
    assert started["framework"] == "pytest"
    assert started["selection"]["worker_count"] == 0

    collected = view.records(event="test_collected")
    assert len(collected) == 10
    assert all(not record["data"]["deselected"] for record in collected)
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 10, "deselected_count": 0}

    # The fixture that fails setup still has both phase finishes: an error
    # setup, then a teardown pair when pytest finalizes it.
    fixture_finishes = [
        (record["data"]["phase"], record["data"]["outcome"])
        for record in view.fixture_events(fixture_id_suffix=".fail_setup")
        if record["event"] == "fixture_finished"
    ]
    assert fixture_finishes == [("setup", "error"), ("teardown", "passed")]

    assert view.manifest["redaction"] == {"applied": False, "findings": 0}
    assert view.manifest["completeness"]["complete"] is True


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_serial_selection_empty_and_error(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)

    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir / "deselected",
        python=python,
        case=_case(name="case_selection.py"),
        extra_args=["-m", "not ci_obs_fast"],
    )
    assert child.returncode == 0
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 2, "deselected_count": 1}
    deselected = [record for record in view.records(event="test_collected") if record["data"]["deselected"]]
    assert len(deselected) == 1
    # The raw -m expression is private text: only its safe hash is recorded.
    assert (
        deselected[0]["data"]["deselection_reason"]
        == f"marker filter: {_filter_identity(expression='not ci_obs_fast')}"
    )

    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir / "empty", python=python, case=_case(name="case_empty.py")
    )
    assert child.returncode == 5
    assert view.records(event="collection_finished")[0]["data"] == {
        "outcome": "completed",
        "collected_count": 0,
        "deselected_count": 0,
    }

    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir / "nomatch",
        python=python,
        case=_case(name="case_selection.py"),
        extra_args=["-m", "nomatch"],
    )
    assert child.returncode == 5
    assert view.records(event="collection_finished")[0]["data"] == {
        "outcome": "completed",
        "collected_count": 2,
        "deselected_count": 2,
    }
    invocation = view.records(event="invocation_finished")[0]["data"]
    assert invocation["outcome"] == "failed"
    assert invocation["exit_code"] == 5

    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir / "broken", python=python, case=_case(name="case_broken.py")
    )
    assert child.returncode == 2
    assert view.records(event="collection_finished")[0]["data"] == {"outcome": "error"}
    assert view.records(event="test_collected") == []
    assert view.manifest["completeness"]["complete"] is True


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_serial_failfast(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_failfast.py"), extra_args=["-x"]
    )
    assert child.returncode == 1
    assert view.records(event="collection_finished")[0]["data"]["collected_count"] == 3
    ran = {key[0] for key in view.phases()}
    assert ran == {_case(name="case_failfast.py") + "::test_first_failure"}


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_fixture_lifecycles_and_finalizer_order(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)
    order_on = work_dir / "order-on.txt"
    order_off = work_dir / "order-off.txt"

    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_fixtures.py"),
        env={"CI_OBS_ORDER_FILE": str(order_on)},
    )
    assert child.returncode == 0

    off = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            f"--junitxml={work_dir / 'junit-off.xml'}",
            _case(name="case_fixtures.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(extra={"CI_OBS_ORDER_FILE": str(order_off)}),
    )
    assert off.returncode == child.returncode
    assert _read_lines(path=order_on) == _read_lines(path=order_off), "finalizer order changed with the plugin"

    # Parameterized instances stay distinct and swap exactly once.
    param_events = view.fixture_events(fixture_id_suffix=".parameterized_resource")
    setups = [
        record for record in param_events if record["event"] == "fixture_started" and record["data"]["phase"] == "setup"
    ]
    teardowns = [
        record
        for record in param_events
        if record["event"] == "fixture_finished" and record["data"]["phase"] == "teardown"
    ]
    assert len(setups) == 2
    assert len(teardowns) == 2
    assert len({record["data"]["params_fingerprint"] for record in setups}) == 2
    instances = {record["data"]["fixture_instance_id"] for record in setups}
    assert len(instances) == 2
    # Identify the instances by their observed lifecycle order, not by the
    # lexical order of their (hash-derived) ids: the first setup starts
    # the instance that pytest's reorder kept first.
    ordered_instances = [record["data"]["fixture_instance_id"] for record in setups]
    first, second = ordered_instances[0], ordered_instances[-1]
    first_teardown = next(
        index
        for index, record in enumerate(param_events)
        if record["event"] == "fixture_finished"
        and record["data"]["phase"] == "teardown"
        and record["data"]["fixture_instance_id"] == first
    )
    second_setup = next(
        index
        for index, record in enumerate(param_events)
        if record["event"] == "fixture_started"
        and record["data"]["phase"] == "setup"
        and record["data"]["fixture_instance_id"] == second
    )
    # The first instance is really torn down before the second is set up:
    # a cache transition, not a fabricated pair.
    assert first_teardown < second_setup

    # The cached first instance serves both consuming tests.
    first_edges = [
        record for record in view.records(event="fixture_used") if record["data"]["fixture_instance_id"] == first
    ]
    assert len(first_edges) == 2

    # Setup and finalizer cost are measured separately from the lifetime.
    module_events = view.fixture_events(fixture_id_suffix=".module_resource")
    module_setup = next(
        record
        for record in module_events
        if record["event"] == "fixture_finished" and record["data"]["phase"] == "setup"
    )
    module_teardown = next(
        record
        for record in module_events
        if record["event"] == "fixture_finished" and record["data"]["phase"] == "teardown"
    )
    setup_end = _parse_utc(value=module_setup["recorded_at"])
    teardown_start = _parse_utc(
        value=next(
            record["recorded_at"]
            for record in module_events
            if record["event"] == "fixture_started" and record["data"]["phase"] == "teardown"
        )
    )
    lifetime = (teardown_start - setup_end).total_seconds()
    cost = (module_setup["data"]["duration_ns"] + module_teardown["data"]["duration_ns"]) / 1e9
    assert lifetime > cost, "fixture lifetime was charged as setup or teardown cost"
    assert module_setup["data"]["duration_ns"] >= 40_000_000
    assert module_teardown["data"]["duration_ns"] >= 40_000_000

    # Finalizers registered during the test body are real teardown cost.
    late_setup = next(
        record
        for record in view.fixture_events(fixture_id_suffix=".late_finalizer_factory")
        if record["event"] == "fixture_finished" and record["data"]["phase"] == "setup"
    )
    late_teardown = next(
        record
        for record in view.fixture_events(fixture_id_suffix=".late_finalizer_factory")
        if record["event"] == "fixture_finished" and record["data"]["phase"] == "teardown"
    )
    assert late_teardown["data"]["duration_ns"] >= 200_000_000
    assert late_setup["data"]["duration_ns"] < 100_000_000

    # Dependent fixtures keep pytest's own teardown order.
    order = _read_lines(path=order_on)
    assert order.index("dependent-teardown") < order.index("dependency-teardown")

    assert view.manifest["completeness"]["complete"] is True


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_no_extra_repr_calls(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)
    repr_on = work_dir / "repr-on.txt"
    repr_off = work_dir / "repr-off.txt"

    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_repr.py"),
        env={"CI_OBS_REPR_FILE": str(repr_on)},
    )
    assert child.returncode == 1
    off = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            f"--junitxml={work_dir / 'junit-off.xml'}",
            _case(name="case_repr.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(extra={"CI_OBS_REPR_FILE": str(repr_off)}),
    )
    assert off.returncode == child.returncode
    assert _read_lines(path=repr_on) == _read_lines(path=repr_off), "the producer called repr on parameter values"
    assert repr_on.exists() and _read_lines(path=repr_on), "pytest itself must have called repr for the comparison"

    fingerprints = {
        record["data"]["params_fingerprint"]
        for record in view.fixture_events(fixture_id_suffix=".counting")
        if record["event"] == "fixture_started"
    }
    assert len(fingerprints) == 2


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_operation_records(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    python = _resolve_child(kind=kind)
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_operations.py")
    )
    assert child.returncode == 0

    starts = {record["data"]["name"]: record["data"] for record in view.records(event="operation_started")}
    assert set(starts) == {
        "wait_for_provision",
        "wait_for_cluster",
        "wait_for_node",
        "wait_for_deletion",
        "failing_helper",
    }
    provision = starts["wait_for_provision"]
    assert provision["resource"]["uid"] == "cr-selftest-1"
    assert provision["test"]["test_id"].endswith("test_operation_lifecycle")
    assert provision["timeout_ms"] == 5000
    assert starts["wait_for_node"]["parent_operation_id"] == starts["wait_for_cluster"]["operation_id"]

    finishes = view.records(event="operation_finished")
    timeout_finish = next(
        record for record in finishes if record["data"].get("failure", {}).get("signature") == "DeadlineExceeded"
    )
    assert timeout_finish["data"]["outcome"] == "timeout"
    failed_finish = next(record for record in finishes if record["data"]["outcome"] == "failed")
    assert failed_finish["data"]["failure"]["signature"] == "ValueError"

    progress = view.records(event="operation_progress")
    assert any(record["data"]["observed_state"] == "Provisioning" for record in progress)


def test_two_invocations_one_job(oracle: tuple[object, object], work_dir: Path) -> None:
    harness = JobHarness.create(directory=work_dir)
    first = harness.run(python=sys.executable, args=[_case(name="case_outcomes.py")])
    second = harness.run(python=sys.executable, args=[_case(name="case_failfast.py")])
    assert first.returncode == 1
    assert second.returncode == 1
    bundle = harness.finish(native_reports=[(first.junit, "junit-first.xml"), (second.junit, "junit-second.xml")])
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    controller = view.journals["controller"]
    assert [record["event"] for record in controller] == ["execution_started", "execution_finished"]
    invocation_ids = {record["data"]["invocation_id"] for record in view.records(event="invocation_started")}
    assert len(invocation_ids) == 2
    assert {record["data"]["outcome"] for record in view.records(event="invocation_finished")} == {"failed"}

    manifest_files = {entry["path"] for entry in view.manifest["files"]}
    assert "native-report/junit-first.xml" in manifest_files
    assert "native-report/junit-second.xml" in manifest_files
    assert view.manifest["completeness"]["complete"] is True

    for path in (bundle / "journals").glob("*.jsonl"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((bundle / "journals").stat().st_mode) == 0o700
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700


def test_xdist_two_workers(oracle: tuple[object, object], work_dir: Path) -> None:
    python = _xdist_child()
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_outcomes.py"), extra_args=["-n", "2"]
    )
    assert child.returncode == 1
    _assert_native_unchanged(
        directory=work_dir,
        python=python,
        args=[_case(name="case_outcomes.py")],
        plugin_run=child,
        extra_args=["-n", "2"],
    )

    started = view.records(event="invocation_started")[0]["data"]
    assert started["selection"]["worker_count"] == 2

    workers = view.worker_journals()
    assert len(workers) == 2
    for worker in workers:
        worker_records = view.journals[worker]
        starts = [record for record in worker_records if record["event"] == "worker_started"]
        stops = [record for record in worker_records if record["event"] == "worker_stopped"]
        assert len(starts) == 1
        assert len(stops) == 1
        assert stops[0]["data"]["outcome"] == "completed"
        assert starts[0]["data"]["pid"] > 0
        assert starts[0]["data"]["host"]

    # Exactly one collection definition for the invocation, in the
    # invocation journal, not duplicated per worker.
    assert len(view.records(event="collection_finished")) == 1
    assert view.records(event="collection_finished")[0]["journal_id"] == view.invocation_journal()
    assert view.records(event="collection_finished")[0]["data"]["collected_count"] == 10

    # Phase records carry the worker whose journal they live in.
    phase_workers = set()
    for stem, records in view.journals.items():
        for record in records:
            if record["event"].startswith("test_phase"):
                assert record["data"]["worker_id"] == stem.rsplit("-", 1)[-1]
                phase_workers.add(record["data"]["worker_id"])
    assert phase_workers <= {worker.rsplit("-", 1)[-1] for worker in workers}

    assert view.manifest["completeness"]["complete"] is True


def test_xdist_worker_loss(oracle: tuple[object, object], work_dir: Path) -> None:
    python = _xdist_child()
    env = {"CI_OBS_KILL_WORKER": "1"}
    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_exit.py"),
        extra_args=["-n", "2"],
        env=env,
    )
    assert child.returncode == 1
    _assert_native_unchanged(
        directory=work_dir,
        python=python,
        args=[_case(name="case_exit.py")],
        plugin_run=child,
        extra_args=["-n", "2"],
        env=env,
    )

    # The lost worker's journal keeps dangling phase starts and never got a
    # worker_stopped of its own; the controller records the loss instead.
    workers = view.worker_journals()
    lost = [stem for stem in workers if not any(record["event"] == "worker_stopped" for record in view.journals[stem])]
    assert len(lost) == 1
    lost_records = view.journals[lost[0]]
    started_phases = {
        (record["data"]["test_id"], record["data"]["attempt_index"], record["data"]["phase"])
        for record in lost_records
        if record["event"] == "test_phase_started"
    }
    finished_phases = {
        (record["data"]["test_id"], record["data"]["attempt_index"], record["data"]["phase"])
        for record in lost_records
        if record["event"] == "test_phase_finished"
    }
    assert started_phases - finished_phases, "the lost worker's running phase must stay dangling"

    interrupted = [
        record for record in view.records(event="worker_stopped") if record["data"]["outcome"] == "interrupted"
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["journal_id"] == view.invocation_journal()

    # xdist replaced the lost worker (its default restart budget).
    assert len(workers) >= 3

    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    assert completeness["end_reason"] == "finished"

    _validation, derivation = oracle
    outcomes = {key[0]: result.outcome for key, result in view.derived(derivation=derivation).items()}
    assert outcomes[_case(name="case_exit.py") + "::test_survivor"] == "passed"


def _spawn_interrupted_child(*, python: str, marker: Path, junit: Path, plugin_target: tuple[Path, Path] | None) -> int:
    command = [python, "-m", "pytest", "-o", "addopts="]
    if plugin_target is not None:
        output, context = plugin_target
        command += ["-p", PLUGIN, f"--ci-observability-output={output}", f"--ci-observability-context={context}"]
    command += [f"--junitxml={junit}", _case(name="case_interrupt.py")]
    child = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(extra={"CI_OBS_BODY_DONE": str(marker)}),
    )
    deadline = time.monotonic() + 120
    while not marker.exists():
        assert time.monotonic() < deadline, "child never reached the test body"
        assert child.poll() is None, "child exited before the test body finished"
        time.sleep(0.05)
    time.sleep(0.3)
    child.send_signal(signal.SIGINT)
    child.communicate(timeout=120)
    return child.returncode


def test_sigint_interrupted_teardown(oracle: tuple[object, object], work_dir: Path) -> None:
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    junit = harness.next_junit()
    code = _spawn_interrupted_child(
        python=python,
        marker=work_dir / "body-on.marker",
        junit=junit,
        plugin_target=(harness.root, harness.context_path),
    )
    assert code == 2, f"expected interrupt exit 2, got {code}"
    harness.exit_codes.append(code)
    bundle = harness.finish(end_reason="interrupted", native_reports=[(junit, "junit.xml")])
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    teardown = view.phases()[(_case(name="case_interrupt.py") + "::test_interrupted_teardown", 1)]["teardown"]
    assert teardown["outcome"] == "interrupted"
    invocation = view.records(event="invocation_finished")[0]["data"]
    assert invocation["outcome"] == "cancelled"
    assert invocation["exit_code"] == 2
    # Every record flushed, so the evidence is complete even though the run
    # ended by interruption; the end reason carries that fact.
    assert view.manifest["completeness"]["end_reason"] == "interrupted"
    assert view.records(event="execution_finished")[0]["data"]["outcome"] == "cancelled"

    off_code = _spawn_interrupted_child(
        python=python, marker=work_dir / "body-off.marker", junit=work_dir / "junit-off.xml", plugin_target=None
    )
    assert off_code == 2

    _validation, derivation = oracle
    result = view.derived(derivation=derivation)[(_case(name="case_interrupt.py") + "::test_interrupted_teardown", 1)]
    assert result.outcome == "interrupted"
    assert result.first_failure is None
    assert result.cleanup_failure is None


def test_effective_zero_after_existing_hooks(oracle: tuple[object, object], work_dir: Path) -> None:
    python = _repo_deps_child()
    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case="tests/e2e/core/test_helpers.py",
        extra_args=["-m", "requires_netris"],
        keep_addopts=True,
    )
    assert child.returncode == 5
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished["outcome"] == "completed"
    assert finished["collected_count"] > 0
    assert finished["collected_count"] == finished["deselected_count"]
    started = view.records(event="invocation_started")[0]["data"]
    assert started["selection"]["worker_count"] == 4
    assert view.phases() == {}
    assert view.records(event="invocation_finished")[0]["data"]["outcome"] == "failed"


def test_output_failure_noninterference(oracle: tuple[object, object], work_dir: Path) -> None:
    python = sys.executable
    case = _case(name="case_outcomes.py")
    claims = ExecutionClaims.from_mapping(payload=_claims_payload())
    context_path = work_dir / "context.json"
    context_path.write_text(json.dumps(_claims_payload()), encoding="utf-8")

    # A symlink planted at the journals directory is never written through.
    evil = work_dir / "evil-target"
    evil.mkdir()
    run_root = work_dir / "run"
    (run_root / "bundle").mkdir(parents=True)
    (run_root / "bundle" / "journals").symlink_to(evil)
    with pytest.raises(RuntimeError, match="header was not persisted"):
        write_job_header(
            execution=claims.execution_payload(),
            suite=claims.suite_payload(),
            environment=claims.environment_payload(),
            run_root=run_root,
        )
    on = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            PLUGIN,
            f"--ci-observability-output={run_root}",
            f"--ci-observability-context={context_path}",
            f"--junitxml={work_dir / 'junit-symlink.xml'}",
            case,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    off = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", f"--junitxml={work_dir / 'junit-off.xml'}", case],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert on.returncode == off.returncode
    assert _junit_cases(path=work_dir / "junit-symlink.xml") == _junit_cases(path=work_dir / "junit-off.xml")
    assert list(evil.iterdir()) == [], "the producer wrote through a symlink"
    assert "ci-observability:" in on.stderr
    statuses = list((run_root / "state").glob("writer-status-*.json"))
    assert statuses, "the collection gap was not recorded"
    status = json.loads(statuses[0].read_text(encoding="utf-8"))
    assert status["status"] == "degraded"
    assert status["dropped_records"] >= 1
    assert any("journals-dir-unusable" in message for message in status["errors"])

    # An output path under a regular file cannot become a run root.
    blocker = work_dir / "blocker"
    blocker.write_text("not a directory")
    on = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            PLUGIN,
            f"--ci-observability-output={blocker / 'run'}",
            f"--ci-observability-context={context_path}",
            f"--junitxml={work_dir / 'junit-blocked.xml'}",
            case,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert on.returncode == off.returncode
    assert _junit_cases(path=work_dir / "junit-blocked.xml") == _junit_cases(path=work_dir / "junit-off.xml")
    assert "ci-observability:" in on.stderr

    # A read-only parent directory denies run root creation (EACCES).
    readonly = work_dir / "noperm"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        on = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-p",
                PLUGIN,
                f"--ci-observability-output={readonly / 'run'}",
                f"--ci-observability-context={context_path}",
                f"--junitxml={work_dir / 'junit-perm.xml'}",
                case,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=_child_env(),
        )
        assert on.returncode == off.returncode
        assert _junit_cases(path=work_dir / "junit-perm.xml") == _junit_cases(path=work_dir / "junit-off.xml")
        assert "ci-observability:" in on.stderr
    finally:
        readonly.chmod(0o700)

    # A reused explicit invocation id is a visible contract violation, not
    # a silent merge.
    collision = JobHarness.create(
        directory=work_dir / "collision", claims=_claims_payload(invocation_id="inv-fixed-selftest")
    )
    first = collision.run(python=sys.executable, args=[_case(name="case_failfast.py")])
    second = collision.run(python=sys.executable, args=[_case(name="case_failfast.py")])
    assert first.returncode == second.returncode == 1
    bundle = collision.finish()
    validation, _derivation = oracle
    with pytest.raises(ValueError, match="duplicate-invocation-start"):
        validation.validate_bundle(bundle_dir=bundle)


def test_plugin_noop_when_unconfigured(work_dir: Path) -> None:
    python = sys.executable
    case = _case(name="case_outcomes.py")

    plain = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", f"--junitxml={work_dir / 'junit-plain.xml'}", case],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    loaded = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", "-p", PLUGIN, f"--junitxml={work_dir / 'junit-loaded.xml'}", case],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert loaded.returncode == plain.returncode
    assert _junit_cases(path=work_dir / "junit-loaded.xml") == _junit_cases(path=work_dir / "junit-plain.xml")
    assert "ci-observability:" not in loaded.stderr

    # Loaded with only half the configuration: one warning, still a no-op.
    half = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            PLUGIN,
            f"--ci-observability-output={work_dir / 'half'}",
            f"--junitxml={work_dir / 'junit-half.xml'}",
            case,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert half.returncode == plain.returncode
    assert _junit_cases(path=work_dir / "junit-half.xml") == _junit_cases(path=work_dir / "junit-plain.xml")
    assert "ci-observability:" in half.stderr
    assert not (work_dir / "half").exists()


def test_no_environment_or_secret_leakage(oracle: tuple[object, object], work_dir: Path) -> None:
    python = sys.executable
    harness, _child, _view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_outcomes.py"),
        env={"OSAC_CI_OBS_CANARY_SECRET": CANARY_SECRET},
    )
    for path in list(harness.root.rglob("*")):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            assert CANARY_SECRET not in content, f"secret leaked into {path}"


def test_serial_child_with_inherited_worker_env(oracle: tuple[object, object], work_dir: Path) -> None:
    """A serial child spawned inside an xdist worker must not be misclassified.

    PYTEST_XDIST_WORKER is inherited by every subprocess a worker spawns,
    so worker identity is derived only from xdist's config.workerinput.
    Before that rule, this child was treated as worker gw9: no
    invocation_started, a gw9 journal, and an invocation-id gap.
    """
    python = sys.executable
    env = {"PYTEST_XDIST_WORKER": "gw9", "PYTEST_XDIST_WORKER_COUNT": "4"}
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_outcomes.py"), env=env
    )
    assert child.returncode == 1
    _assert_native_unchanged(
        directory=work_dir, python=python, args=[_case(name="case_outcomes.py")], plugin_run=child, env=env
    )

    assert set(view.journals) == {"controller", view.invocation_journal()}
    assert not any(stem.endswith("-gw9") for stem in view.journals)
    assert view.records(event="invocation_started")[0]["data"]["selection"]["worker_count"] == 0
    assert all(record["data"]["worker_id"] == "main" for record in view.records(event="test_phase_started"))
    status_path = work_dir / "run" / "state" / f"writer-status-{view.invocation_journal()}.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "stopped"
    assert status["dropped_records"] == 0
    assert status["errors"] == []
    assert view.manifest["completeness"]["complete"] is True


def test_nested_in_process_run_two_levels(oracle: tuple[object, object], work_dir: Path) -> None:
    """An unconfigured nested pytest.main stays unrecorded with an explicit gap.

    The outer tracker must survive the inner run: the test after the
    nested call is still recorded, the outer invocation finishes once
    with the outer exit status, and the bundle stays contract-valid but
    not complete, because records were omitted.
    """
    python = sys.executable
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_nested.py")
    )
    assert child.returncode == 0
    _assert_native_unchanged(directory=work_dir, python=python, args=[_case(name="case_nested.py")], plugin_run=child)

    gaps = [
        record
        for record in view.records(event="collection_state")
        if record["data"]["source"] == "pytest-nested-invocation"
    ]
    assert len(gaps) == 1
    assert gaps[0]["data"]["status"] == "unavailable"
    assert gaps[0]["data"]["complete"] is False

    ran = {key[0] for key in view.phases()}
    assert _case(name="case_nested.py") + "::test_spawns_unconfigured_inner_run" in ran
    assert _case(name="case_nested.py") + "::test_after_nested_run" in ran
    assert not any("test_inner_passes" in test_id for test_id in ran)
    assert len(view.records(event="invocation_started")) == 1
    assert view.records(event="invocation_finished")[0]["data"]["exit_code"] == 0

    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    assert completeness["dropped_records"] >= 1
    assert any("nested-invocation-unrecorded" in error["message"] for error in completeness["collection_errors"])


def test_nested_in_process_run_three_levels(oracle: tuple[object, object], work_dir: Path) -> None:
    """Three levels: the mid-level records itself, the innermost leaves a gap.

    Exercises the per-config suspend map: the mid-level run activates its
    own tracker while the outer one is suspended, and each level restores
    exactly the tracker below it.
    """
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    env = {"CI_OBS_DEEP_OUTPUT": str(harness.root), "CI_OBS_DEEP_CONTEXT": str(harness.context_path)}
    child = harness.run(python=python, args=[_case(name="case_nested_deep.py")], env=env)
    assert child.returncode == 0
    bundle = harness.finish()
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    outer_stem = next(
        stem
        for stem, records in view.journals.items()
        if any(
            record["event"] == "test_phase_started" and "test_three_level_nesting" in record["data"]["test_id"]
            for record in records
        )
    )
    mid_stem = next(
        stem
        for stem, records in view.journals.items()
        if any(
            record["event"] == "test_phase_started" and "test_mid_level" in record["data"]["test_id"]
            for record in records
        )
    )
    assert outer_stem != mid_stem
    gap_journals = [
        stem
        for stem, records in view.journals.items()
        if any(
            record["event"] == "collection_state" and record["data"].get("source") == "pytest-nested-invocation"
            for record in records
        )
    ]
    assert gap_journals == [mid_stem]

    assert len(view.records(event="invocation_started")) == 2
    assert len(view.records(event="invocation_finished")) == 2
    ran = {key[0] for key in view.phases()}
    assert _case(name="case_nested_deep.py") + "::test_after_deep_nesting" in ran
    assert not any("test_inner_passes" in test_id for test_id in ran)
    assert view.manifest["completeness"]["complete"] is False


def test_collection_includes_items_added_by_hooks(oracle: tuple[object, object], work_dir: Path) -> None:
    """Items another collection hook adds must be collected and counted.

    Added items run, so they belong to test_collected and the effective
    counts; otherwise the contract's executed-not-collected check would
    fail and the collection would undercount.
    """
    python = sys.executable
    env = {"CI_OBS_ADD_ITEMS": "1"}
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_selection.py"), env=env
    )
    assert child.returncode == 0
    _assert_native_unchanged(
        directory=work_dir, python=python, args=[_case(name="case_selection.py")], plugin_run=child, env=env
    )

    added_id = _case(name="case_selection.py") + "::test_added_dynamically"
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 3, "deselected_count": 0}
    assert added_id in {record["data"]["test_id"] for record in view.records(event="test_collected")}
    assert any(key[0] == added_id for key in view.phases())
    assert view.manifest["completeness"]["complete"] is True


def _journal_records(*, directory: Path, journal_id: str) -> list[dict[str, object]]:
    path = directory / f"{journal_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _summary_lines(*, stdout: str) -> list[str]:
    """The terminal result summary lines of a pytest run, without timing."""
    return [
        line.split(" in ")[0]
        for line in stdout.splitlines()
        if line.startswith("====") and ("passed" in line or "error" in line)
    ]


def test_worker_without_id_never_claims_main(oracle: tuple[object, object], work_dir: Path) -> None:
    """A real worker whose workerinput lacks a worker id must not record as main.

    A tryfirst shim sets ``config.workerinput = {}`` while the child still
    inherits a controller invocation id: exactly the state of a worker
    whose id never arrived. Records carry the explicit unknown marker,
    the counted gap reaches the writer status and the manifest, and the
    native run is unchanged. The shim itself also suppresses JUnit
    (pytest's junitxml skips workers) and disables the cache, so the
    native comparison uses exit code and terminal summary, and asserts
    JUnit is equally absent with and without the plugin. The bundle is
    not contract-validated here: the standalone shim has no controller
    to open the invocation, an artifact of the simulation rather than of
    the producer.
    """
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    env = {"CI_OBS_EMPTY_WORKERINPUT": "1", "OSAC_CI_OBS_INVOCATION_ID": "inv-shim"}
    child = harness.run(python=python, args=[_case(name="case_outcomes.py")], env=env)
    assert child.returncode == 1
    off = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            f"--junitxml={work_dir / 'junit-off.xml'}",
            _case(name="case_outcomes.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(extra=env),
    )
    assert off.returncode == child.returncode
    assert _summary_lines(stdout=child.stdout) == _summary_lines(stdout=off.stdout)
    assert child.junit is not None and not child.junit.exists()
    assert not (work_dir / "junit-off.xml").exists()

    bundle = harness.finish()
    view = _load_bundle(bundle=bundle)

    assert not any(stem.endswith("-main") for stem in view.journals)
    assert [stem for stem in view.journals if stem != "controller"] == ["inv-shim-unknown"]
    records = view.journals["inv-shim-unknown"]
    assert not any(record["event"] == "invocation_started" for record in records)
    started = [record for record in records if record["event"] == "worker_started"]
    assert len(started) == 1
    assert started[0]["data"]["worker_id"] == "unknown"
    assert all(
        record["data"]["worker_id"] == "unknown" for record in records if record["event"].startswith("test_phase")
    )

    status = json.loads((harness.root / "state" / "writer-status-inv-shim-unknown.json").read_text(encoding="utf-8"))
    assert status["status"] == "degraded"
    assert status["dropped_records"] >= 1
    assert "worker-id-unavailable" in status["errors"]

    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    assert any("worker-id-unavailable" in error["message"] for error in completeness["collection_errors"])


def test_journal_write_failure_mid_run(oracle: tuple[object, object], work_dir: Path) -> None:
    """A real mid-journal write failure through a file-size fault boundary.

    RLIMIT_FSIZE with SIGXFSZ ignored turns the journal's growth into
    EFBIG write errors mid-run. The run's exit code and JUnit stay
    unchanged, the torn line is removed so the journal stays valid JSON
    Lines, and the loss surfaces as a counted, visible gap. A privileged
    ENOSPC mount is not available here; this boundary exercises the same
    writer failure path without privileges.
    """
    python = sys.executable
    limit = 16 * 1024
    wrapper = (
        "import resource, signal, sys;\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({limit}, {limit}));\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN);\n"
        "import pytest;\n"
        "sys.exit(pytest.console_main())\n"
    )
    command = [python, "-c", wrapper, "-o", "addopts=", "-o", "log_file="]
    harness = JobHarness.create(directory=work_dir)
    on = subprocess.run(
        [
            *command,
            "-p",
            PLUGIN,
            f"--ci-observability-output={harness.root}",
            f"--ci-observability-context={harness.context_path}",
            f"--junitxml={harness.next_junit()}",
            _case(name="case_outcomes.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    off = subprocess.run(
        [*command, f"--junitxml={work_dir / 'junit-off.xml'}", _case(name="case_outcomes.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert on.returncode == 1
    assert off.returncode == 1
    assert _junit_cases(path=harness.junit_dir / "junit-1.xml") == _junit_cases(path=work_dir / "junit-off.xml")

    harness.exit_codes.append(on.returncode)
    bundle = harness.finish()
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    journal = next(path for path in (bundle / "journals").glob("inv-*.jsonl"))
    assert journal.stat().st_size <= limit
    for line in journal.read_text(encoding="utf-8").splitlines():
        json.loads(line)

    status_path = harness.root / "state" / f"writer-status-{journal.stem}.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "degraded"
    assert status["dropped_records"] >= 1
    assert "journal-write-failed-efbig" in status["errors"]

    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    assert completeness["dropped_records"] >= 1
    assert any("journal-write-failed-efbig" in error["message"] for error in completeness["collection_errors"])


def test_parameter_values_never_recorded(oracle: tuple[object, object], work_dir: Path) -> None:
    """Raw parameter values are replaced by safe hashes in every producer output.

    Default pytest ids embed the parameter value verbatim and custom ids
    built from it leak the same way; every journal, sidecar, status file,
    manifest, operation test ref, and the plugin's own stderr must carry
    only the hashed form, while the native JUnit and console keep the raw
    ids unchanged.
    """
    python = sys.executable
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_params.py")
    )
    assert child.returncode == 0
    _assert_native_unchanged(directory=work_dir, python=python, args=[_case(name="case_params.py")], plugin_run=child)
    # Native output keeps the raw ids; only producer files are sanitized.
    assert "SECRETTOKEN-canary" in child.junit.read_text(encoding="utf-8")

    for path in (
        list((work_dir / "run" / "bundle" / "journals").glob("*.jsonl"))
        + list((work_dir / "run" / "state").glob("*.json"))
        + [work_dir / "run" / "bundle" / "bundle-manifest.json"]
    ):
        content = path.read_text(encoding="utf-8")
        assert "SECRETTOKEN-canary" not in content, f"raw parameter leaked into {path.name}"
    assert "SECRETTOKEN-canary" not in child.stderr

    # The safe id is the pinned hash form, identical across collected,
    # phase, use-edge, and operation test refs.
    expected_custom = (
        _case(name="case_params.py")
        + "::test_param_custom_ids[p:"
        + __import__("hashlib").sha256(b"ID-SECRETTOKEN-canary").hexdigest()[:12]
        + "]"
    )
    collected = {record["data"]["test_id"] for record in view.records(event="test_collected")}
    assert expected_custom in collected
    phase_ids = {key[0] for key in view.phases()}
    assert expected_custom in phase_ids
    use_ids = {record["data"]["test_id"] for record in view.records(event="fixture_used")}
    assert use_ids <= collected
    operation_refs = {
        record["data"]["test"]["test_id"]
        for record in view.records(event="operation_started")
        if "test" in record["data"]
    }
    assert operation_refs <= collected
    assert operation_refs and operation_refs <= phase_ids

    # Distinct parameter values stay distinct.
    default_ids = {test_id for test_id in collected if "::test_param_default_ids[" in test_id}
    assert len(default_ids) == 2


def test_parameter_ids_stable_across_runs_and_interpreters(oracle: tuple[object, object], work_dir: Path) -> None:
    """Safe ids are deterministic across runs and both child interpreters."""
    ids_by_run: dict[str, set[str]] = {}
    for label, python in (("session", sys.executable), ("alt", _resolve_child(kind="alt"))):
        for run in (1, 2):
            directory = work_dir / f"{label}-{run}"
            _harness, _child, view = _run_validated_job(
                oracle=oracle, directory=directory, python=python, case=_case(name="case_params.py")
            )
            ids_by_run[f"{label}-{run}"] = {
                record["data"]["test_id"] for record in view.records(event="test_collected")
            }
    assert len(ids_by_run) == 4
    assert len({frozenset(ids) for ids in ids_by_run.values()}) == 1


def test_cli_selection_node_ids_sanitized(oracle: tuple[object, object], work_dir: Path) -> None:
    """A CLI node id with a parameter must not leak it into selection.paths."""
    python = sys.executable
    node_id = _case(name="case_params.py") + "::test_param_custom_ids[ID-SECRETTOKEN-canary]"
    _harness, child, view = _run_validated_job(oracle=oracle, directory=work_dir, python=python, case=node_id)
    assert child.returncode == 0
    selection = view.records(event="invocation_started")[0]["data"]["selection"]
    assert len(selection["paths"]) == 1
    assert "SECRETTOKEN-canary" not in selection["paths"][0]
    assert selection["paths"][0].endswith("]")
    assert "[p:" in selection["paths"][0]


def _run_with_context_path(
    *, python: str, directory: Path, context_path: Path, timeout: float = 60.0
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Run one child against an existing context path, plus a plugin-off run."""
    case = _case(name="case_selection.py")
    on = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            PLUGIN,
            f"--ci-observability-output={directory / 'run'}",
            f"--ci-observability-context={context_path}",
            case,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )
    off = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", case],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )
    return on, off


def _run_with_context_document(
    *, python: str, directory: Path, document: bytes, timeout: float = 60.0
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Run one child with a hand-crafted context document, plus a plugin-off run."""
    context_path = directory / "context.json"
    context_path.write_bytes(document)
    return _run_with_context_path(python=python, directory=directory, context_path=context_path, timeout=timeout)


def _assert_malformed_context_handled(
    *, on: subprocess.CompletedProcess[str], off: subprocess.CompletedProcess[str], canary: str | None = None
) -> None:
    """A malformed context leaves the run native, warn once, and leak nothing."""
    assert on.returncode == off.returncode, f"exit changed: {on.returncode} != {off.returncode}"
    assert "INTERNALERROR" not in on.stdout
    warnings = [line for line in on.stderr.splitlines() if line.startswith("ci-observability:")]
    assert len(warnings) == 1, f"expected exactly one warning, got: {warnings}"
    if canary is not None:
        assert canary not in on.stdout
        assert canary not in on.stderr


def test_malformed_context_documents(oracle: tuple[object, object], work_dir: Path) -> None:
    """Malformed context documents never crash the run and never echo content."""
    python = sys.executable
    good = _claims_payload()

    cases: list[tuple[str, bytes, str | None]] = [
        ("null-document", b"null", None),
        ("list-document", b"[]", None),
        ("framework-list", json.dumps({**good, "suite": {**good["suite"], "framework": []}}).encode(), None),
        ("cohort-list", json.dumps({**good, "cohort": []}).encode(), None),
        ("deep-nesting", b"[" * 50000 + b"]" * 50000, None),
        ("unknown-key", json.dumps({**good, "unknown-SECRETTOKEN-canary": 1}).encode(), "SECRETTOKEN-canary"),
        ("nonfinite-value", b'{"execution_id": NaN}', None),
        ("duplicate-keys", b'{"execution_id": "a", "execution_id": "b"}', None),
        (
            "bad-role",
            json.dumps(
                {**good, "sources": [{**good["sources"][0], "role": "not-a-role"}, good["sources"][1]]}
            ).encode(),
            None,
        ),
        (
            "role-list",
            json.dumps({**good, "sources": [{**good["sources"][0], "role": []}, good["sources"][1]]}).encode(),
            None,
        ),
        ("oversize", json.dumps({**good, "filler": "x" * (2 * 1024 * 1024)}).encode(), None),
    ]
    for name, document, canary in cases:
        directory = work_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        on, off = _run_with_context_document(python=python, directory=directory, document=document)
        _assert_malformed_context_handled(on=on, off=off, canary=canary)


def test_context_fifo_fails_fast(oracle: tuple[object, object], work_dir: Path) -> None:
    """A FIFO at the context path must not block configure."""
    python = sys.executable
    directory = work_dir / "fifo"
    directory.mkdir(parents=True, exist_ok=True)
    context_path = directory / "context.json"
    os.mkfifo(context_path)
    started = time.monotonic()
    on, off = _run_with_context_path(python=python, directory=directory, context_path=context_path, timeout=60.0)
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"configure blocked on the FIFO for {elapsed:.1f}s"
    _assert_malformed_context_handled(on=on, off=off)


def test_native_report_copy_never_clobbers(oracle: tuple[object, object], work_dir: Path) -> None:
    """Native report copies are exclusive: symlinks and existing files are refused."""
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    child = harness.run(python=python, args=[_case(name="case_selection.py")])
    assert child.returncode == 0

    outside = work_dir / "outside-target"
    outside.write_text("outside content must survive", encoding="utf-8")
    report_dir = harness.root / "bundle" / "native-report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "junit.xml").symlink_to(outside)
    with pytest.raises(RuntimeError, match="native report target already exists"):
        harness.finish(native_reports=[(child.junit, "junit.xml")])
    assert outside.read_text(encoding="utf-8") == "outside content must survive"
    assert not (report_dir / "junit.xml").is_symlink() or (report_dir / "junit.xml").exists()

    (report_dir / "junit.xml").unlink(missing_ok=True)
    (report_dir / "junit.xml").write_text("existing regular content", encoding="utf-8")
    with pytest.raises(RuntimeError, match="native report target already exists"):
        harness.finish(native_reports=[(child.junit, "junit.xml")])
    assert (report_dir / "junit.xml").read_text(encoding="utf-8") == "existing regular content"

    # The plugin-off comparison still matches the child's native result.
    off = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            f"--junitxml={work_dir / 'junit-off.xml'}",
            _case(name="case_selection.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert off.returncode == child.returncode
    assert _junit_cases(path=child.junit) == _junit_cases(path=work_dir / "junit-off.xml")


def test_manifest_write_failure_visible(oracle: tuple[object, object], work_dir: Path) -> None:
    """A directory planted at the manifest path makes finish fail loudly."""
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    child = harness.run(python=python, args=[_case(name="case_selection.py")])
    assert child.returncode == 0
    (harness.root / "bundle" / "bundle-manifest.json").mkdir()
    with pytest.raises(RuntimeError, match="manifest was not persisted"):
        harness.finish()
    # The native child result is unaffected by the bootstrap failure.
    off = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", _case(name="case_selection.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert off.returncode == child.returncode


def test_truncate_failure_disables_journal(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed truncate recovery permanently stops the journal.

    The persisted prefix stays valid JSON Lines, no record lands after
    the failure, and the gap is counted with a fixed label.
    """
    writer = JournalWriter(
        run_dirs=RunDirs(journals=work_dir, state=work_dir), journal_id="truncate", execution_id="exec-x"
    )
    assert writer.emit(
        event="collection_state", data={"source": "x", "status": "stopped", "complete": True, "dropped_records": 0}
    )
    real_write = os.write

    def failing_write(fd: int, data: object) -> int:
        raise OSError(errno.EIO, "injected write failure")

    def failing_truncate(fd: int, length: int) -> None:
        raise OSError(errno.EIO, "injected truncate failure")

    monkeypatch.setattr(os, "write", failing_write)
    monkeypatch.setattr(os, "ftruncate", failing_truncate)
    assert not writer.emit(
        event="collection_state", data={"source": "x", "status": "collecting", "complete": False, "dropped_records": 0}
    )
    monkeypatch.setattr(os, "write", real_write)
    # The journal is permanently disabled: even a recovered write path
    # must not append after the failed recovery.
    assert not writer.emit(
        event="collection_state", data={"source": "x", "status": "stopped", "complete": True, "dropped_records": 0}
    )
    assert writer.dropped_records >= 2
    assert "journal-truncate-failed" in writer.errors
    writer.close(status="degraded")

    lines = (work_dir / "truncate.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        json.loads(line)
    assert len(lines) == 1


def test_nested_operations_scoped_per_invocation(oracle: tuple[object, object], work_dir: Path) -> None:
    """An inner run cannot use an outer operation as a parent, and the outer registry survives.

    The outer test starts an operation, runs a configured nested pytest
    whose test tries to use it as a parent (rejected: per-invocation
    registry), and then completes an outer parent-child pair, proving the
    outer registry was restored. The inner records never cross-link the
    outer invocation.
    """
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    env = {"CI_OBS_OPS_OUTPUT": str(harness.root), "CI_OBS_OPS_CONTEXT": str(harness.context_path)}
    child = harness.run(python=python, args=[_case(name="case_nested_outer_ops.py")], env=env)
    assert child.returncode == 0
    bundle = harness.finish()
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    outer_stem = next(
        stem
        for stem, records in view.journals.items()
        if any(
            record["event"] == "operation_started" and record["data"]["operation_id"] == "op-outer-fixed"
            for record in records
        )
    )
    inner_stem = next(stem for stem in view.journals if stem not in {outer_stem, "controller"})
    outer_records = view.journals[outer_stem]
    inner_records = view.journals[inner_stem]

    # The outer registry was restored: parent and child both recorded.
    outer_operations = [record["data"] for record in outer_records if record["event"] == "operation_started"]
    assert {record["operation_id"] for record in outer_operations} >= {"op-outer-fixed"}
    outer_child_ids = {
        record["data"]["parent_operation_id"]
        for record in outer_records
        if record["event"] == "operation_started" and "parent_operation_id" in record["data"]
    }
    assert "op-outer-fixed" in outer_child_ids

    # The inner invocation recorded no operations: its only test rejected
    # the outer parent before emitting anything.
    assert not any(record["event"].startswith("operation_") for record in inner_records)
    assert not any("op-outer-fixed" in json.dumps(record) for record in inner_records)


ADVERSARIAL_CASE = "case_adversarial_ids.py"
ADVERSARIAL_CANARY = "EN-canary"


def _adversarial_raw_ids() -> dict[str, str]:
    """Raw node ids of the adversarial case, by short name."""
    prefix = f"{FIXTURES}/{ADVERSARIAL_CASE}::"
    return {
        "default_colon": f"{prefix}test_default_colon_id[fe80::{ADVERSARIAL_CANARY}]",
        "custom_colon": f"{prefix}test_custom_colon_id[TOK::{ADVERSARIAL_CANARY}]",
        "open_bracket": f"{prefix}test_custom_open_bracket_id[{ADVERSARIAL_CANARY}[brk]",
        "close_bracket": f"{prefix}test_custom_close_bracket_id[brk]{ADVERSARIAL_CANARY}]",
        # pytest ascii-escapes non-ASCII in ids, so the node id carries
        # literal backslash-u escape text, not the raw characters.
        "unicode": f"{prefix}test_unicode_id[{ADVERSARIAL_CANARY}-\\u03a9-\\u2713]",
        "operation": f"{prefix}test_operation_under_adversarial_id[{ADVERSARIAL_CANARY}]",
        "plain_control": f"{prefix}test_plain_control[plain-value]",
    }


def _assert_no_canary_in_producer_output(*, harness_root: Path, stderr: str) -> None:
    """The canary is absent from every producer file and from producer stderr."""
    for path in (
        list((harness_root / "bundle" / "journals").glob("*.jsonl"))
        + list((harness_root / "state").glob("*.json"))
        + [harness_root / "bundle" / "bundle-manifest.json"]
    ):
        content = path.read_text(encoding="utf-8")
        assert ADVERSARIAL_CANARY not in content, f"raw parameter leaked into {path.name}"
    assert ADVERSARIAL_CANARY not in stderr


def test_adversarial_parameter_ids_never_leak(oracle: tuple[object, object], work_dir: Path) -> None:
    """Parameter payloads containing ::, brackets, and Unicode are fully hashed.

    The first ``::`` and the first ``[`` after the test name are the
    structural boundaries; separators inside a parameter value must not
    trick the sanitizer into leaving part of the payload raw. The native
    JUnit keeps the raw ids.
    """
    python = sys.executable
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name=ADVERSARIAL_CASE)
    )
    assert child.returncode == 0
    _assert_native_unchanged(directory=work_dir, python=python, args=[_case(name=ADVERSARIAL_CASE)], plugin_run=child)
    assert ADVERSARIAL_CANARY in child.junit.read_text(encoding="utf-8")
    _assert_no_canary_in_producer_output(harness_root=work_dir / "run", stderr=child.stderr)

    raw_ids = _adversarial_raw_ids()
    expected = {safe_test_id(test_id=raw) for raw in raw_ids.values()}
    collected = {record["data"]["test_id"] for record in view.records(event="test_collected")}
    assert collected == expected
    # Distinct adversarial parameters stay distinct.
    assert len(collected) == len(raw_ids)

    # Every boundary agrees on the same safe id for the same raw node id.
    phase_ids = {key[0] for key in view.phases()}
    use_ids = {record["data"]["test_id"] for record in view.records(event="fixture_used")}
    operation_refs = {
        record["data"]["test"]["test_id"]
        for record in view.records(event="operation_started")
        if "test" in record["data"]
    }
    assert phase_ids == expected
    # pytest delivers parametrize values through a per-test fixture, so
    # every parametrized test has use edges; they must all carry safe ids
    # drawn from the same set, and the resource-fixture consumer keeps
    # its own edge.
    assert use_ids <= expected
    assert safe_test_id(test_id=raw_ids["default_colon"]) in use_ids
    assert operation_refs == {safe_test_id(test_id=raw_ids["operation"])}
    assert all("[p:" in test_id for test_id in collected)


def test_adversarial_ids_stable_across_interpreters(oracle: tuple[object, object], work_dir: Path) -> None:
    """Adversarial safe ids are deterministic across both child interpreters."""
    expected = {safe_test_id(test_id=raw) for raw in _adversarial_raw_ids().values()}
    for kind in ("session", "alt"):
        python = _resolve_child(kind=kind)
        _harness, _child, view = _run_validated_job(
            oracle=oracle, directory=work_dir / kind, python=python, case=_case(name=ADVERSARIAL_CASE)
        )
        collected = {record["data"]["test_id"] for record in view.records(event="test_collected")}
        assert collected == expected


def test_cli_selection_adversarial_node_id(oracle: tuple[object, object], work_dir: Path) -> None:
    """A CLI node id whose parameter contains :: must not leak into selection.paths."""
    python = sys.executable
    node_id = _adversarial_raw_ids()["custom_colon"]
    _harness, child, view = _run_validated_job(oracle=oracle, directory=work_dir, python=python, case=node_id)
    assert child.returncode == 0
    selection = view.records(event="invocation_started")[0]["data"]["selection"]
    assert len(selection["paths"]) == 1
    assert ADVERSARIAL_CANARY not in selection["paths"][0]
    assert selection["paths"][0] == safe_test_id(test_id=node_id)
    assert view.records(event="collection_finished")[0]["data"]["collected_count"] == 1


def test_xdist_adversarial_ids_sidecar_agreement(oracle: tuple[object, object], work_dir: Path) -> None:
    """Under xdist, worker sidecars and controller crosschecks stay safe-vs-safe.

    Adversarial ids must not trip a false catalog mismatch, the single
    collection definition must count all seven tests, and the bundle must
    validate against the frozen contract.
    """
    python = _xdist_child()
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name=ADVERSARIAL_CASE), extra_args=["-n", "2"]
    )
    assert child.returncode == 0
    _assert_no_canary_in_producer_output(harness_root=work_dir / "run", stderr=child.stderr)

    finished = view.records(event="collection_finished")
    assert len(finished) == 1
    assert finished[0]["data"] == {"outcome": "completed", "collected_count": 7, "deselected_count": 0}
    collected = {record["data"]["test_id"] for record in view.records(event="test_collected")}
    assert collected == {safe_test_id(test_id=raw) for raw in _adversarial_raw_ids().values()}
    for sidecar in (work_dir / "run" / "state").glob("collection-*.json"):
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert all(ADVERSARIAL_CANARY not in test_id for test_id in payload.get("collected", []))


def test_xdist_sidecar_crosscheck_detects_mismatch(work_dir: Path) -> None:
    """The controller's safe-vs-safe catalog crosscheck still detects divergence.

    A stub config drives the real Tracker: matching safe ids emit a
    completed collection; a reported id whose safe form diverges from the
    sidecar emits an unknown outcome with a fixed-label mismatch error.
    """
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=2), args=[])
    claims = ExecutionClaims.from_mapping(payload=_claims_payload(invocation_id="inv-xcheck"))
    tracker = Tracker(config=config, claims=claims, run_root=work_dir / "run")
    raw_id = _adversarial_raw_ids()["custom_colon"]
    safe_id = safe_test_id(test_id=raw_id)
    write_collection_sidecar(
        state_dir=tracker.run_dirs.state,
        journal_id="inv-xcheck-gw0",
        payload={"collected": [safe_id], "deselected": [], "error": False},
    )
    node = SimpleNamespace(gateway=SimpleNamespace(id="gw0"))

    tracker.emit_xdist_collection(node=node, ids=[raw_id])
    records = _journal_records(directory=work_dir / "run" / "bundle" / "journals", journal_id="inv-xcheck")
    assert records[-1]["event"] == "collection_finished"
    assert records[-1]["data"] == {"outcome": "completed", "collected_count": 1, "deselected_count": 0}
    assert any(record["event"] == "test_collected" and record["data"]["test_id"] == safe_id for record in records)

    tracker.collection_emitted = False
    tracker.emit_xdist_collection(node=node, ids=[raw_id + "-diverged"])
    records = _journal_records(directory=work_dir / "run" / "bundle" / "journals", journal_id="inv-xcheck")
    assert records[-1]["event"] == "collection_finished"
    assert records[-1]["data"] == {"outcome": "unknown"}
    assert "worker-collection-metadata-mismatch" in tracker.writer.errors
    assert tracker.writer.dropped_records >= 1
    tracker.writer.close(status="degraded")


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_native_copy_closes_descriptors(oracle: tuple[object, object], work_dir: Path) -> None:
    """Every descriptor a native-report copy opens is closed on every path.

    The reviewer measured one leaked destination descriptor per copy, on
    success and on the failure paths; the footer runs in this process, so
    the process's own fd count is the oracle.
    """
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    child = harness.run(python=python, args=[_case(name="case_selection.py")])
    assert child.returncode == 0
    source = work_dir / "copied-report.xml"
    shutil.copyfile(child.junit, source)
    target = work_dir / "bundle" / "native-report" / "junit.xml"
    target.parent.mkdir(parents=True)

    before = _fd_count()
    JobJournal._copy_exclusive(source=source, target=target)
    assert _fd_count() == before
    assert target.read_bytes() == source.read_bytes()

    # The failure path (a target that already exists) must not leak either.
    before2 = _fd_count()
    with pytest.raises(RuntimeError, match="native report target already exists"):
        JobJournal._copy_exclusive(source=source, target=target)
    assert _fd_count() == before2

    # And the public footer path closes its descriptors too: the writer's
    # own journal descriptor is the only one finish closes by design.
    harness3 = JobHarness.create(directory=work_dir / "third")
    child3 = harness3.run(python=python, args=[_case(name="case_selection.py")])
    assert child3.returncode == 0
    before3 = _fd_count()
    harness3.finish(native_reports=[(child3.junit, "junit.xml")])
    assert _fd_count() == before3 - 1  # the controller journal descriptor


def test_native_copy_rejects_fifo_after_precheck(work_dir: Path) -> None:
    """A FIFO at the source cannot block the copy, before or after the precheck.

    The public precheck rejects a FIFO it can see; the open itself still
    carries O_NONBLOCK and a post-open regular check for the window
    between the precheck and the open, which this drives directly with a
    FIFO at the source path (exactly the post-precheck state).
    """
    source = work_dir / "report.json"
    os.mkfifo(source)
    target = work_dir / "bundle" / "native-report" / "junit.xml"
    target.parent.mkdir(parents=True)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="native report source is not a regular file"):
        JobJournal._copy_exclusive(source=source, target=target)
    assert time.monotonic() - started < 10, "the copy blocked on a FIFO"

    python = sys.executable
    harness = JobHarness.create(directory=work_dir / "public")
    child = harness.run(python=python, args=[_case(name="case_selection.py")])
    assert child.returncode == 0
    fifo = work_dir / "public" / "fifo-report"
    os.mkfifo(fifo)
    with pytest.raises(RuntimeError, match="native report source is not a regular file"):
        harness.finish(native_reports=[(fifo, "junit.xml")])
    off = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", _case(name="case_selection.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert off.returncode == child.returncode


def test_metadata_write_never_clobbers_foreign_inodes(oracle: tuple[object, object], work_dir: Path) -> None:
    """A planted hardlink or FIFO at a metadata path survives untouched.

    The writer-status file name is deterministic for an explicit
    invocation id, so a hardlink to an outside file can be planted before
    the child runs. The exclusive write refuses it, the outside bytes
    survive, and the missing status surfaces as a visible gap in the
    manifest instead of a guessed sum or a crash.
    """
    python = sys.executable
    outside = work_dir / "outside-status"
    outside.write_text("ORIGINAL-CONTENT", encoding="utf-8")
    harness = JobHarness.create(directory=work_dir / "hardlink", claims=_claims_payload(invocation_id="inv-metadata"))
    status_path = harness.root / "state" / "writer-status-inv-metadata.json"
    os.link(outside, status_path)
    child = harness.run(python=python, args=[_case(name="case_selection.py")])
    assert child.returncode == 0
    bundle = harness.finish()
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)
    assert outside.read_text(encoding="utf-8") == "ORIGINAL-CONTENT"
    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    assert any(error["message"] == "writer-status-missing" for error in completeness["collection_errors"])

    # A FIFO at a metadata path is refused fast, never opened or blocked.
    harness2 = JobHarness.create(directory=work_dir / "fifo", claims=_claims_payload(invocation_id="inv-fifo-meta"))
    fifo_status = harness2.root / "state" / "writer-status-inv-fifo-meta.json"
    os.mkfifo(fifo_status)
    started = time.monotonic()
    child2 = harness2.run(python=python, args=[_case(name="case_selection.py")])
    assert child2.returncode == 0
    bundle2 = harness2.finish()
    _validate_bundle(oracle=oracle, bundle=bundle2)
    assert time.monotonic() - started < 120, "a metadata write blocked on a FIFO"
    view2 = _load_bundle(bundle=bundle2)
    assert view2.manifest["completeness"]["complete"] is False


def test_malformed_sidecar_degrades_not_crashes(work_dir: Path) -> None:
    """A wrong-shaped worker sidecar degrades collection with a fixed-label gap.

    Real xdist corruption is simulated by planting malformed sidecars
    under the deterministic worker journal names before an actual xdist
    child run: the worker's own exclusive write refuses to overwrite them
    (a counted gap), and the controller's guarded reader rejects the
    wrong shape instead of crashing or trusting it.
    """
    python = _xdist_child()
    invocation = "inv-corrupt"
    harness = JobHarness.create(directory=work_dir, claims=_claims_payload(invocation_id=invocation))
    state = harness.root / "state"
    state.mkdir(parents=True, exist_ok=True)
    # Wrong shapes: a string where a list belongs, and deselected entries
    # that are plain strings instead of objects with a test_id.
    (state / f"collection-{invocation}-gw0.json").write_text(
        json.dumps({"collected": "not-a-list", "deselected": []}), encoding="utf-8"
    )
    (state / f"collection-{invocation}-gw1.json").write_text(
        json.dumps({"collected": ["a"], "deselected": ["plain-string"]}), encoding="utf-8"
    )
    child = harness.run(python=python, args=[_case(name="case_selection.py")], extra_args=["-n", "2"])
    assert child.returncode == 0
    bundle = harness.finish()
    _validate_bundle(oracle=_oracle_or_fail(), bundle=bundle)
    view = _load_bundle(bundle=bundle)
    finished = view.records(event="collection_finished")
    assert len(finished) == 1
    assert finished[0]["data"]["outcome"] == "unknown"
    completeness = view.manifest["completeness"]
    assert completeness["complete"] is False
    messages = [error["message"] for error in completeness["collection_errors"]]
    assert any("worker-collection-metadata-missing" in message for message in messages)
    assert any("collection-sidecar-write-failed" in message for message in messages)


def _oracle_or_fail() -> tuple[object, object]:
    """The frozen oracle for helper paths that cannot take a fixture.

    Unset skips visibly, like the module fixture; configured but broken
    fails.
    """
    raw = os.environ.get(CONTRACT_ENV)
    if not raw:
        pytest.skip(f"{CONTRACT_ENV} is not set: frozen-contract oracle unavailable")
    contract_dir = Path(raw).resolve()
    repo_root = contract_dir.parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from monitoring.ci_observability.contracts import derivation, validation

    return validation, derivation


def test_operation_owner_affinity_in_process(work_dir: Path) -> None:
    """Operations emit only into the journal that started them.

    Wrong-invocation calls neither emit nor consume the finish; an
    inert-started operation stays inert after activation; progress after
    a terminal finish creates no records.
    """
    (work_dir / "a").mkdir()
    (work_dir / "b").mkdir()
    first = JournalWriter(
        run_dirs=RunDirs(journals=work_dir / "a", state=work_dir / "a"), journal_id="own-a", execution_id="exec-x"
    )
    second = JournalWriter(
        run_dirs=RunDirs(journals=work_dir / "b", state=work_dir / "b"), journal_id="own-b", execution_id="exec-x"
    )
    observability_context._bind_state(journal=first, operations=set())
    operation = OperationContext(name="affine", expected_condition="done", operation_id="op-affine")
    operation.start()
    operation.progress(observed_state="in owner")

    # A different invocation is active: no orphan records, no consumption.
    observability_context._bind_state(journal=second, operations=set())
    operation.progress(observed_state="wrong context")
    operation.finish(outcome="passed")

    # Restored to the owning invocation: the finish still works.
    observability_context._bind_state(journal=first, operations=set())
    operation.finish(outcome="passed")
    # Progress after the terminal finish creates nothing.
    operation.progress(observed_state="after finish")
    observability_context._bind_state(journal=None, operations=None)

    records_a = _journal_records(directory=work_dir / "a", journal_id="own-a")
    events_a = [record["event"] for record in records_a]
    assert "operation_started" in events_a
    assert events_a.count("operation_progress") == 1
    assert events_a.count("operation_finished") == 1
    assert not _journal_records(directory=work_dir / "b", journal_id="own-b")

    # An inert start stays inert for its whole life, even after activation.
    inert = OperationContext(name="inert", expected_condition="never", operation_id="op-inert")
    inert.start()
    observability_context._bind_state(journal=first, operations=set())
    inert.progress(observed_state="activated late")
    inert.finish(outcome="passed")
    observability_context._bind_state(journal=None, operations=None)
    after = [record["event"] for record in _journal_records(directory=work_dir / "a", journal_id="own-a")]
    assert after.count("operation_started") == 1  # only the affine operation
    first.close(status="stopped")
    second.close(status="stopped")


def test_operation_affinity_real_nested_run(oracle: tuple[object, object], work_dir: Path) -> None:
    """A real configured nested run cannot use or consume an outer operation.

    The outer invocation starts an operation, a nested configured run
    calls progress and finish on its handle, and the outer invocation
    still finishes it correctly afterwards; the inner journal carries no
    operation records at all.
    """
    python = sys.executable
    harness = JobHarness.create(directory=work_dir)
    env = {"CI_OBS_AFFINITY_OUTPUT": str(harness.root), "CI_OBS_AFFINITY_CONTEXT": str(harness.context_path)}
    child = harness.run(python=python, args=[_case(name="case_nested_ops_outer.py")], env=env)
    assert child.returncode == 0
    bundle = harness.finish()
    _validate_bundle(oracle=oracle, bundle=bundle)
    view = _load_bundle(bundle=bundle)

    outer_stem = next(
        stem
        for stem, records in view.journals.items()
        if any(
            record["event"] == "operation_started" and record["data"]["operation_id"] == "op-affinity-outer"
            for record in records
        )
    )
    outer_records = view.journals[outer_stem]
    inner_stems = [stem for stem in view.journals if stem not in {outer_stem, "controller"}]
    assert len(inner_stems) == 1
    inner_records = view.journals[inner_stems[0]]

    # The inner invocation recorded no operation records.
    assert not any(record["event"].startswith("operation_") for record in inner_records)
    assert not any("op-affinity-outer" in json.dumps(record) for record in inner_records)

    # The outer operation was started, progressed, and finished once, in
    # the outer invocation, after the nested run returned.
    outer_operations = [record for record in outer_records if record["event"].startswith("operation_")]
    assert [record["event"] for record in outer_operations] == [
        "operation_started",
        "operation_progress",
        "operation_finished",
    ]
    assert outer_operations[-1]["data"]["outcome"] == "passed"


def test_fixture_override_definitions_distinct(oracle: tuple[object, object], work_dir: Path) -> None:
    """Same-name fixture definitions keep distinct stable identities.

    A module-scoped fixture and a class-scoped override of the same
    argname in one module are two definitions: two fixture ids and two
    instance ids. The module definition is cached across its users (one
    setup lifetime, several use edges), the class override is set up once
    inside its class, and the finalizer order is identical with the
    plugin off.
    """
    python = sys.executable
    order_on = work_dir / "order-on.txt"
    order_off = work_dir / "order-off.txt"
    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_fixture_override.py"),
        env={"CI_OBS_ORDER_FILE": str(order_on)},
    )
    assert child.returncode == 0
    off = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            f"--junitxml={work_dir / 'junit-off.xml'}",
            _case(name="case_fixture_override.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(extra={"CI_OBS_ORDER_FILE": str(order_off)}),
    )
    assert off.returncode == child.returncode
    assert _read_lines(path=order_on) == _read_lines(path=order_off), "finalizer order changed with the plugin"

    setups = [
        (record["data"]["fixture_id"], record["data"]["fixture_instance_id"])
        for record in view.fixture_events(fixture_id_suffix="shared_name")
        if record["event"] == "fixture_started" and record["data"]["phase"] == "setup"
    ]
    # Two distinct definitions (module-level and class override) of the
    # same argname: distinct ids, distinct instances, one setup each.
    assert len(setups) == 2
    assert len({fixture_id for fixture_id, _instance in setups}) == 2
    assert len({instance for _fixture_id, instance in setups}) == 2

    # The module definition is cached: its single instance serves both
    # module-level tests; the class override serves both class tests.
    use_edges = [
        record
        for record in view.records(event="fixture_used")
        if record["data"]["fixture_instance_id"] in {instance for _f, instance in setups}
    ]
    assert len(use_edges) == 4
    per_instance: dict[str, int] = {}
    for record in use_edges:
        per_instance[record["data"]["fixture_instance_id"]] = (
            per_instance.get(record["data"]["fixture_instance_id"], 0) + 1
        )
    assert sorted(per_instance.values()) == [2, 2]

    # The order log proves which definition served which test.
    order = _read_lines(path=order_on)
    assert order.count("module-shared-name-setup") == 1
    assert order.count("class-shared-name-setup") == 1
    assert order.count("class-shared-name-class-setup") == 1


def test_phase_end_excludes_report_delay(oracle: tuple[object, object], work_dir: Path) -> None:
    """Report-generation latency is not charged to the phase that finished.

    A trylast makereport hook sleeps 200ms after every report and marks a
    file, proving the delay ran; the producer's call duration must stay
    near the native duration (the reviewer measured the whole 200ms
    charged to a 0ms body before the fix).
    """
    python = sys.executable
    marker = work_dir / "delay-marker.txt"
    env = {"CI_OBS_REPORT_DELAY_MS": "200", "CI_OBS_REPORT_DELAY_MARKER": str(marker)}
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=_case(name="case_outcomes.py"), env=env
    )
    assert child.returncode == 1
    # The delay hook really ran: one line per report of every test.
    assert len(_read_lines(path=marker)) >= 20

    junit = ET.parse(child.junit).getroot()
    native_times = [
        float(case.get("time", "0")) for case in junit.iter("testcase") if case.get("name") == "test_passes"
    ]
    phases = view.phases()
    call = phases[(f"{FIXTURES}/case_outcomes.py::test_passes", 1)]["call"]
    producer_ms = call["duration_ns"] / 1_000_000
    assert producer_ms < 100, f"report delay charged to the body: {producer_ms:.0f}ms"
    assert native_times and native_times[0] < 0.5


def test_strict_xpass_classified_unexpected_pass(oracle: tuple[object, object], work_dir: Path) -> None:
    """Strict unexpected passes stay unexpected passes on both pytest versions.

    A strict xfail that passes is reported by pytest as a plain failure
    without wasxfail; the evaluated xfail state on the item is what
    distinguishes it. A condition that does not hold, --runxfail, and a
    real failure under an xfail mark keep their native outcomes, and the
    native exit codes are unchanged.
    """
    case = _case(name="case_strict_xpass.py")
    prefix = f"{FIXTURES}/case_strict_xpass.py::"
    for kind in ("session", "alt"):
        python = _resolve_child(kind=kind)
        _harness, child, view = _run_validated_job(oracle=oracle, directory=work_dir / kind, python=python, case=case)
        assert child.returncode == 1, f"{kind}: native strict-XPASS exit changed"
        outcomes = {key[0]: result.outcome for key, result in view.derived(derivation=oracle[1]).items()}
        assert outcomes[prefix + "test_strict_xpass"] == "unexpected_pass"
        assert outcomes[prefix + "test_strict_xpass_condition_not_applied"] == "passed"
        assert outcomes[prefix + "test_non_strict_xpass"] == "unexpected_pass"
        assert outcomes[prefix + "test_strict_xfail_wrong_exception"] == "failed"
        assert outcomes[prefix + "test_plain_pass"] == "passed"

    # --runxfail disables the marks: the strict XPASS is an ordinary pass.
    python = _resolve_child(kind="alt")
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir / "runxfail", python=python, case=case, extra_args=["--runxfail"]
    )
    assert child.returncode == 1  # the wrong-exception test still genuinely fails
    outcomes = {key[0]: result.outcome for key, result in view.derived(derivation=oracle[1]).items()}
    assert outcomes[prefix + "test_strict_xpass"] == "passed"
    assert outcomes[prefix + "test_strict_xfail_wrong_exception"] == "failed"


def test_output_path_symlink_loop_stays_inactive(work_dir: Path) -> None:
    """An output path that cannot be resolved leaves the run native.

    Path.resolve raises RuntimeError (not OSError) on a symlink loop; the
    producer must catch it, warn once with a fixed label, and stay
    inactive instead of an internal error.
    """
    python = sys.executable
    loop = work_dir / "loop"
    loop.mkdir()
    os.symlink(loop / "a", loop / "b")
    os.symlink(loop / "b", loop / "a")
    context_path = work_dir / "context.json"
    context_path.write_text(json.dumps(_claims_payload()), encoding="utf-8")
    case = _case(name="case_selection.py")
    on = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            PLUGIN,
            f"--ci-observability-output={loop / 'a' / 'run'}",
            f"--ci-observability-context={context_path}",
            case,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    off = subprocess.run(
        [python, "-m", "pytest", "-o", "addopts=", case],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=_child_env(),
    )
    assert on.returncode == off.returncode
    assert "INTERNALERROR" not in on.stdout
    warnings = [line for line in on.stderr.splitlines() if line.startswith("ci-observability:")]
    assert len(warnings) == 1
    # The refusal surfaces at the first guarded boundary: the resolve
    # guard on Python versions where resolve() raises, or the run-root
    # creation guard where it does not. Both are fixed labels, one
    # warning, and the run stays native.
    assert (
        "output path could not be resolved" in warnings[0] or "journals unavailable (run-root-unusable-" in warnings[0]
    )


def test_context_field_type_validation() -> None:
    """Known context fields with wrong types or bounds are refused."""
    good = _claims_payload()

    def expect_error(*, payload: dict[str, object], fragment: str) -> None:
        with pytest.raises(ContextError, match=fragment):
            ExecutionClaims.from_mapping(payload=payload)

    component = dict(good)
    component["suite"] = {**good["suite"], "component": []}
    expect_error(payload=component, fragment="context-suite-component-invalid")

    short_component = dict(good)
    short_component["suite"] = {**good["suite"], "component": ""}
    expect_error(payload=short_component, fragment="context-suite-component-invalid")

    variant = dict(good)
    variant["suite"] = {**good["suite"], "variant": 7}
    expect_error(payload=variant, fragment="context-suite-variant-invalid")

    boolean_job_id = dict(good)
    boolean_job_id["job"] = {**good["job"], "github_job_id": True}
    expect_error(payload=boolean_job_id, fragment="context-github-job-id-invalid")

    zero_job_id = dict(good)
    zero_job_id["job"] = {**good["job"], "github_job_id": 0}
    expect_error(payload=zero_job_id, fragment="context-github-job-id-invalid")

    dependencies = dict(good)
    dependencies["dependencies"] = ["ok", ""]
    expect_error(payload=dependencies, fragment="context-dependencies-invalid")

    long_dependencies = dict(good)
    long_dependencies["dependencies"] = ["x" * 65]
    expect_error(payload=long_dependencies, fragment="context-dependencies-invalid")

    many_dependencies = dict(good)
    many_dependencies["dependencies"] = [f"d{i}" for i in range(17)]
    expect_error(payload=many_dependencies, fragment="context-dependencies-invalid")


def _assert_no_canary_in_selector_outputs(*, harness_root: Path, stderr: str) -> None:
    for path in (
        list((harness_root / "bundle" / "journals").glob("*.jsonl"))
        + list((harness_root / "state").glob("*.json"))
        + [harness_root / "bundle" / "bundle-manifest.json"]
    ):
        content = path.read_text(encoding="utf-8")
        assert "EN-canary" not in content, f"raw selector text leaked into {path.name}"
    assert "EN-canary" not in stderr


def test_selector_filter_privacy_serial(oracle: tuple[object, object], work_dir: Path) -> None:
    """-k and -m expressions are recorded only as safe hashes, serially.

    The expression can quote parameter values verbatim, so the raw text
    never reaches a producer file; the safe identity, selected membership
    and counts still are.
    """
    python = sys.executable
    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir / "keyword",
        python=python,
        case=_case(name="case_selector_canary.py"),
        extra_args=["-k", "EN-canary"],
    )
    assert child.returncode == 0
    _assert_no_canary_in_selector_outputs(harness_root=work_dir / "keyword" / "run", stderr=child.stderr)
    selection = view.records(event="invocation_started")[0]["data"]["selection"]
    assert selection["keyword"] == _filter_identity(expression="EN-canary")
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 3, "deselected_count": 2}
    deselected = [record for record in view.records(event="test_collected") if record["data"]["deselected"]]
    assert len(deselected) == 2
    expected_reason = f"keyword filter: {_filter_identity(expression='EN-canary')}"
    assert all(record["data"]["deselection_reason"] == expected_reason for record in deselected)

    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir / "marker",
        python=python,
        case=_case(name="case_selector_canary.py"),
        extra_args=["-m", "EN-canary"],
    )
    assert child.returncode == 0
    _assert_no_canary_in_selector_outputs(harness_root=work_dir / "marker" / "run", stderr=child.stderr)
    selection = view.records(event="invocation_started")[0]["data"]["selection"]
    assert selection["markers"] == _filter_identity(expression="EN-canary")
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 3, "deselected_count": 2}
    ran = {key[0] for key in view.phases()}
    assert ran == {f"{FIXTURES}/case_selector_canary.py::test_canary_marked"}
    deselected = [record for record in view.records(event="test_collected") if record["data"]["deselected"]]
    expected_marker_reason = f"marker filter: {_filter_identity(expression='EN-canary')}"
    assert all(record["data"]["deselection_reason"] == expected_marker_reason for record in deselected)


def test_selector_filter_privacy_xdist(oracle: tuple[object, object], work_dir: Path) -> None:
    """Under xdist, worker sidecars and controller records hash selectors too."""
    python = _xdist_child()
    _harness, child, view = _run_validated_job(
        oracle=oracle,
        directory=work_dir,
        python=python,
        case=_case(name="case_selector_canary.py"),
        extra_args=["-n", "2", "-k", "EN-canary"],
    )
    assert child.returncode == 0
    _assert_no_canary_in_selector_outputs(harness_root=work_dir / "run", stderr=child.stderr)
    selection = view.records(event="invocation_started")[0]["data"]["selection"]
    assert selection["keyword"] == _filter_identity(expression="EN-canary")
    finished = view.records(event="collection_finished")[0]["data"]
    assert finished == {"outcome": "completed", "collected_count": 3, "deselected_count": 2}
    deselected = [record for record in view.records(event="test_collected") if record["data"]["deselected"]]
    assert len(deselected) == 2
    assert all(
        record["data"]["deselection_reason"] == f"keyword filter: {_filter_identity(expression='EN-canary')}"
        for record in deselected
    )


def _write_conftest_tree(*, root: Path, order_file: Path) -> None:
    """A parent/child conftest tree whose fixtures truly collide.

    Without an ``__init__.py`` chain, pytest loads both conftest modules
    as bare ``conftest`` with the same qualname: module and qualname
    alone cannot tell the definitions apart, which is exactly the
    colliding case the reviewer reproduced.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "conftest.py").write_text(
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(scope='module')\n"
        "def shared_fixture():\n"
        "    with open(os.environ['CI_OBS_ORDER_FILE'], 'a') as handle:\n"
        "        handle.write('parent-setup\\n')\n"
        "    return 'parent'\n",
        encoding="utf-8",
    )
    (root / "case_parent_tests.py").write_text(
        "def test_parent_first(shared_fixture):\n"
        "    assert shared_fixture == 'parent'\n"
        "\n"
        "\n"
        "def test_parent_second(shared_fixture):\n"
        "    assert shared_fixture == 'parent'\n",
        encoding="utf-8",
    )
    child = root / "nested"
    child.mkdir(exist_ok=True)
    (child / "conftest.py").write_text(
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(scope='module')\n"
        "def shared_fixture():\n"
        "    with open(os.environ['CI_OBS_ORDER_FILE'], 'a') as handle:\n"
        "        handle.write('child-setup\\n')\n"
        "    return 'child'\n",
        encoding="utf-8",
    )
    (child / "case_child_tests.py").write_text(
        "def test_child_first(shared_fixture):\n"
        "    assert shared_fixture == 'child'\n"
        "\n"
        "\n"
        "def test_child_second(shared_fixture):\n"
        "    assert shared_fixture == 'child'\n",
        encoding="utf-8",
    )
    del order_file


@pytest.mark.parametrize("kind", ["session", "alt"])
def test_direct_param_fixture_lifetimes_distinct(kind: str, oracle: tuple[object, object], work_dir: Path) -> None:
    """Each actual parametrize setup is its own fixture lifetime.

    pytest creates a fresh FixtureDef per parametrize mark, all sharing
    module ``_pytest.python`` and qualname
    ``get_direct_param_fixture_func`` with a root baseid, so two tests
    parametrizing the same argument name at the same index carry the
    same definition identity — with different values or the same value.
    Every real setup must get a distinct ``fixture_instance_id`` with
    exactly one setup/teardown pair and a correctly targeted use edge,
    while the cached user fixture stays one lifetime for its two tests.
    """
    python = _resolve_child(kind=kind)
    case = _case(name="case_direct_params.py")
    _harness, child, view = _run_validated_job(oracle=oracle, directory=work_dir / kind, python=python, case=case)
    assert child.returncode == 0
    _assert_native_unchanged(directory=work_dir / kind, python=python, args=[case], plugin_run=child)

    direct_param_id = "_pytest.python.get_direct_param_fixture_func"
    events = [record for record in view.fixture_events(fixture_id_suffix=direct_param_id)]
    setups = [
        record for record in events if record["event"] == "fixture_started" and record["data"]["phase"] == "setup"
    ]
    teardowns = [
        record for record in events if record["event"] == "fixture_started" and record["data"]["phase"] == "teardown"
    ]
    # Five parametrized items: two crossed marks with two values each,
    # plus the same-value mark — five actual setup lifetimes.
    assert len(setups) == 5
    instance_ids = {record["data"]["fixture_instance_id"] for record in events}
    assert len(instance_ids) == 5, "distinct parametrize lifetimes shared one fixture_instance_id"

    # One setup/teardown pair per lifetime id, nothing dangling.
    per_id: dict[str, dict[str, int]] = {}
    for record in events:
        instance = record["data"]["fixture_instance_id"]
        key = f"{record['event']}:{record['data']['phase']}"
        counts = per_id.setdefault(instance, {})
        counts[key] = counts.get(key, 0) + 1
    for instance, counts in per_id.items():
        assert counts == {
            "fixture_started:setup": 1,
            "fixture_finished:setup": 1,
            "fixture_started:teardown": 1,
            "fixture_finished:teardown": 1,
        }, f"lifetime {instance} is not exactly one setup/teardown pair: {counts}"
    assert sorted(record["data"]["fixture_instance_id"] for record in teardowns) == sorted(
        record["data"]["fixture_instance_id"] for record in setups
    )

    # Use edges: every parametrized test targets its own direct-param
    # lifetime, and the cached user fixture is one lifetime used twice.
    param_edges = [
        record for record in view.records(event="fixture_used") if record["data"]["fixture_instance_id"] in instance_ids
    ]
    assert len(param_edges) == 5
    assert len({record["data"]["fixture_instance_id"] for record in param_edges}) == 5
    user_events = view.fixture_events(fixture_id_suffix="case_direct_params.shared_user_resource")
    user_instances = {record["data"]["fixture_instance_id"] for record in user_events}
    assert len(user_instances) == 1, "the cached user fixture must stay one lifetime"
    user_counts: dict[str, int] = {}
    for record in user_events:
        key = f"{record['event']}:{record['data']['phase']}"
        user_counts[key] = user_counts.get(key, 0) + 1
    assert user_counts == {
        "fixture_started:setup": 1,
        "fixture_finished:setup": 1,
        "fixture_started:teardown": 1,
        "fixture_finished:teardown": 1,
    }
    user_edges = [
        record
        for record in view.records(event="fixture_used")
        if record["data"]["fixture_instance_id"] in user_instances
    ]
    # Both crossed marks' four items share the one cached lifetime.
    assert len(user_edges) == 4
    assert len({record["data"]["fixture_instance_id"] for record in user_edges}) == 1


def test_direct_param_lifetimes_distinct_under_xdist(oracle: tuple[object, object], work_dir: Path) -> None:
    """The same distinct-lifetime contract holds per worker under xdist."""
    python = _xdist_child()
    case = _case(name="case_direct_params.py")
    _harness, child, view = _run_validated_job(
        oracle=oracle, directory=work_dir, python=python, case=case, extra_args=["-n", "2"]
    )
    assert child.returncode == 0

    events = view.fixture_events(fixture_id_suffix="_pytest.python.get_direct_param_fixture_func")
    setups = [
        record for record in events if record["event"] == "fixture_started" and record["data"]["phase"] == "setup"
    ]
    assert len(setups) == 5
    # Ordinals are per worker process, and the worker id is hashed into
    # the instance id, so the five lifetimes stay distinct even when
    # workers split them.
    assert len({record["data"]["fixture_instance_id"] for record in events}) == 5
    edges = [
        record
        for record in view.records(event="fixture_used")
        if record["data"]["fixture_instance_id"] in {record["data"]["fixture_instance_id"] for record in events}
    ]
    assert len(edges) == 5
    assert len({record["data"]["fixture_instance_id"] for record in edges}) == 5


def test_conftest_override_definitions_distinct(oracle: tuple[object, object], work_dir: Path) -> None:
    """Parent and child conftest definitions of one argname stay distinct.

    Both conftest functions load as module ``conftest`` with the same
    qualname and scope, so only the fixture's root-relative namespace
    (``FixtureDef.baseid``) separates them. The bundle must show two
    distinct definition/instance pairs, each cached definition serving
    its two tests with one setup lifetime, with native outcomes and
    finalizer order unchanged, on both pytest versions.
    """
    claims = _claims_payload()
    for kind in ("session", "alt"):
        python = _resolve_child(kind=kind)
        tree = work_dir / kind
        order_on = tree / "order-on.txt"
        order_off = tree / "order-off.txt"
        _write_conftest_tree(root=tree, order_file=order_on)

        context_path = tree / "context.json"
        context_path.write_text(json.dumps(claims), encoding="utf-8")
        parsed = ExecutionClaims.from_mapping(payload=claims)
        run_root = tree / "run"
        header = write_job_header(
            execution=parsed.execution_payload(),
            suite=parsed.suite_payload(),
            environment=parsed.environment_payload(),
            run_root=run_root,
        )
        env = {"CI_OBS_ORDER_FILE": str(order_on), "PYTHONPATH": str(REPO_ROOT)}
        on = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-p",
                PLUGIN,
                f"--ci-observability-output={run_root}",
                f"--ci-observability-context={context_path}",
                f"--junitxml={tree / 'junit-on.xml'}",
                "case_parent_tests.py",
                "nested/case_child_tests.py",
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=300,
            env={**_child_env(), **env},
        )
        assert on.returncode == 0, on.stdout[-2000:]

        off = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                f"--junitxml={tree / 'junit-off.xml'}",
                "case_parent_tests.py",
                "nested/case_child_tests.py",
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=300,
            env={**_child_env(), "CI_OBS_ORDER_FILE": str(order_off)},
        )
        assert off.returncode == on.returncode
        assert _junit_cases(path=tree / "junit-on.xml") == _junit_cases(path=tree / "junit-off.xml")
        assert _read_lines(path=order_on) == _read_lines(path=order_off), "finalizer order changed with the plugin"
        assert sorted(_read_lines(path=order_on)) == ["child-setup", "parent-setup"]

        bundle = header.finish(outcome="passed", exit_code=on.returncode, end_reason="finished")
        _validate_bundle(oracle=oracle, bundle=bundle)
        view = _load_bundle(bundle=bundle)
        setups = [
            (record["data"]["fixture_id"], record["data"]["fixture_instance_id"])
            for record in view.records(event="fixture_started")
            if record["data"]["phase"] == "setup"
        ]
        teardowns = [
            record["data"]["fixture_instance_id"]
            for record in view.records(event="fixture_finished")
            if record["data"]["phase"] == "teardown"
        ]
        # Two definitions, two instances: the collision the reviewer
        # reproduced (same module, same qualname, same scope, first
        # counter value each) must not merge them.
        assert len(setups) == 2
        assert len(set(setups)) == 2
        assert sorted(teardowns) == sorted(instance for _fid, instance in setups)

        # Each cached definition serves its two tests: two use edges per
        # instance, four in total.
        edges = view.records(event="fixture_used")
        assert len(edges) == 4
        per_instance: dict[str, int] = {}
        for record in edges:
            instance = record["data"]["fixture_instance_id"]
            per_instance[instance] = per_instance.get(instance, 0) + 1
        assert sorted(per_instance.values()) == [2, 2]

        # The displayed identities differ and are safe dotted forms.
        assert len({fixture_id for fixture_id, _instance in setups}) == 2
        for fixture_id, _instance in setups:
            assert fixture_id.endswith(".conftest.shared_fixture") or fixture_id.endswith("conftest.shared_fixture")


def _write_two_layout_tree(*, root: Path) -> None:
    """Two directory layouts whose conftest fixtures must not collide.

    Layout A uses ``parent/child`` (a nested directory); layout B uses
    ``parent.child`` (a directory literally named with a dot). Both
    conftest functions load as module ``conftest`` with qualname
    ``shared_fixture``, and their dotted display paths are identical
    (``parent.child``); only the raw root-relative baseid separates
    them, which the digest-based canonical identity must reflect.
    """
    layouts = [("parent/child", "layout-a"), ("parent.child", "layout-b")]
    for relative, label in layouts:
        directory = root / label / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "conftest.py").write_text(
            f"import pytest\n\n\n@pytest.fixture(scope='module')\ndef shared_fixture():\n    return '{label}'\n",
            encoding="utf-8",
        )
        (directory / f"case_{label}_tests.py").write_text(
            f"def test_layout(shared_fixture):\n    assert shared_fixture == '{label}'\n", encoding="utf-8"
        )


def test_two_layout_conftest_collision_distinct(oracle: tuple[object, object], work_dir: Path) -> None:
    """parent/child and parent.child conftests stay two definitions.

    Both conftest functions share module ``conftest`` and qualname
    ``shared_fixture``, and their dotted display paths are identical.
    The canonical identity derives from the raw root-relative baseid, so
    the digest (not the display form) must separate them. Both pytest
    versions must produce two distinct definition/instance pairs with
    cached sharing, valid bundles, and native outcomes.
    """
    claims = _claims_payload()
    for kind in ("session", "alt"):
        python = _resolve_child(kind=kind)
        tree = work_dir / kind
        _write_two_layout_tree(root=tree)

        context_path = tree / "context.json"
        context_path.write_text(json.dumps(claims), encoding="utf-8")
        parsed = ExecutionClaims.from_mapping(payload=claims)
        run_root = tree / "run"
        header = write_job_header(
            execution=parsed.execution_payload(),
            suite=parsed.suite_payload(),
            environment=parsed.environment_payload(),
            run_root=run_root,
        )
        on = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-p",
                PLUGIN,
                f"--ci-observability-output={run_root}",
                f"--ci-observability-context={context_path}",
                f"--junitxml={tree / 'junit-on.xml'}",
                "layout-a/parent/child/case_layout-a_tests.py",
                "layout-b/parent.child/case_layout-b_tests.py",
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=300,
            env={**_child_env(), "PYTHONPATH": str(REPO_ROOT)},
        )
        assert on.returncode == 0, on.stdout[-2000:]

        off = subprocess.run(
            [
                python,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                f"--junitxml={tree / 'junit-off.xml'}",
                "layout-a/parent/child/case_layout-a_tests.py",
                "layout-b/parent.child/case_layout-b_tests.py",
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=300,
            env=_child_env(),
        )
        assert off.returncode == on.returncode
        assert _junit_cases(path=tree / "junit-on.xml") == _junit_cases(path=tree / "junit-off.xml")

        bundle = header.finish(outcome="passed", exit_code=on.returncode, end_reason="finished")
        _validate_bundle(oracle=oracle, bundle=bundle)
        view = _load_bundle(bundle=bundle)
        setups = [
            (record["data"]["fixture_id"], record["data"]["fixture_instance_id"])
            for record in view.records(event="fixture_started")
            if record["data"]["phase"] == "setup"
        ]
        # Two layouts, two definitions, two instances: the digest of the
        # raw baseid separates them even though the dotted display path
        # is identical.
        assert len(setups) == 2
        assert len(set(setups)) == 2
        assert len({fixture_id for fixture_id, _instance in setups}) == 2
        assert len({instance for _fixture_id, instance in setups}) == 2

        # Each cached definition serves its own test.
        edges = view.records(event="fixture_used")
        assert len(edges) == 2


def test_new_code_helpers_are_keyword_only() -> None:
    """Every new non-protocol helper takes its data arguments keyword-only.

    Signatures that an external protocol calls positionally are exempt by
    explicit qualified name or an explicit protocol rule, never by kind:
    a new helper with a positional data parameter, constructor included,
    fails here. The gate parses source text only; it reads no git state
    and touches no network.
    """
    # pluggy hook specifications: pytest dispatches these by hook name.
    pytest_hooks = frozenset(
        {
            "pytest_addoption",
            "pytest_configure",
            "pytest_unconfigure",
            "pytest_sessionstart",
            "pytest_sessionfinish",
            "pytest_configure_node",
            "pytest_testnodedown",
            "pytest_xdist_node_collection_finished",
            "pytest_collectreport",
            "pytest_collection_modifyitems",
            "pytest_collection_finish",
            "pytest_runtest_protocol",
            "pytest_runtest_setup",
            "pytest_runtest_call",
            "pytest_runtest_teardown",
            "pytest_runtest_makereport",
            "pytest_fixture_setup",
        }
    )
    protocol_signatures = frozenset(
        {
            # The class-level replacement for _pytest.fixtures.FixtureDef.finish.
            "plugin._install_finish_wrap.finish",
            # json.loads callbacks: parse_constant and object_pairs_hook.
            "context._reject_constant",
            "context._strict_pairs",
            # Context manager protocol.
            "context.OperationContext.__exit__",
            # os.write / os.ftruncate stand-ins installed by monkeypatch.
            "tests.test_truncate_failure_disables_journal.failing_write",
            "tests.test_truncate_failure_disables_journal.failing_truncate",
        }
    )
    modules = {
        "plugin": REPO_ROOT / "tests" / "e2e" / "core" / "observability" / "plugin.py",
        "writer": REPO_ROOT / "tests" / "e2e" / "core" / "observability" / "writer.py",
        "context": REPO_ROOT / "tests" / "e2e" / "core" / "observability" / "context.py",
        "tests": Path(__file__),
    }

    def is_exempt(*, module: str, qualified: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if qualified in protocol_signatures:
            return True
        if module == "plugin" and "." not in qualified.removeprefix("plugin.") and node.name in pytest_hooks:
            return True
        if module == "tests":
            # pytest injects fixture arguments into test functions and
            # fixture functions by name.
            if node.name.startswith("test_"):
                return True
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "fixture"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "pytest"
                ):
                    return True
        return False

    violations: list[str] = []

    def check(*, module: str, qualified: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        params = [*node.args.posonlyargs, *node.args.args]
        if params and params[0].arg in ("self", "cls"):
            params = params[1:]
        if params:
            violations.append(f"{qualified}: positional data parameter(s) {[param.arg for param in params]}")

    def visit(*, module: str, node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified = f"{prefix}.{child.name}"
                if not is_exempt(module=module, qualified=qualified, node=child):
                    check(module=module, qualified=qualified, node=child)
                visit(module=module, node=child, prefix=qualified)
            elif isinstance(child, ast.ClassDef):
                visit(module=module, node=child, prefix=f"{prefix}.{child.name}")
            else:
                visit(module=module, node=child, prefix=prefix)

    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visit(module=module, node=tree, prefix=module)

    assert not violations, "\n".join(violations)
