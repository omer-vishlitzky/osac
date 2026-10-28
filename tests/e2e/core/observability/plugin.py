"""The CI observability pytest plugin (W02 producer).

Explicit opt-in only: load with ``-p tests.e2e.core.observability.plugin``
and pass both a private output directory and a trusted context document
(``--ci-observability-output``/``--ci-observability-context`` options or
``OSAC_CI_OBS_OUTPUT``/``OSAC_CI_OBS_CONTEXT`` variables). Loaded but not
fully configured, the plugin is a no-op.

Per pytest invocation (one command line, all of its xdist workers sharing
the invocation id) it owns the invocation, worker, collection, test phase,
and fixture journals. It never writes ``execution_started``,
``execution_finished``, or the manifest: those belong to the trusted
workflow bootstrap (W04, or ``writer.write_job_header``/``JobJournal.finish``
for a standalone harness). Observer failures are visible collection gaps
and never change the native run; test ids carry parameter values only as
safe hashes; journals stay private and unredacted until W06, and the
manifest claims ``redaction.applied: false``.

Private pytest API used for real finalizer measurement (verified against
pytest 8.3.5 and 9.1.1): a reference-counted class-level wrap of
``_pytest.fixtures.FixtureDef.finish`` that only measures around the
original call (LIFO finalizer order untouched, late-registered finalizers
covered); ``FixtureDef.cached_result`` to skip no-op finish calls; a
passive read of ``FixtureRequest._fixture_defs`` for use edges;
``pytest_fixture_setup`` cache-miss semantics; and the public xdist hooks
``pytest_configure_node``, ``pytest_testnodedown``, and
``pytest_xdist_node_collection_finished``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

try:
    from _pytest.fixtures import FixtureDef
except ImportError:  # pragma: no cover - private pytest internals moved
    FixtureDef = None  # type: ignore[assignment,misc]

try:
    from _pytest.skipping import xfailed_key
except ImportError:  # pragma: no cover - private pytest internals moved
    xfailed_key = None  # type: ignore[assignment]

from tests.e2e.core.observability import context as obs_context
from tests.e2e.core.observability.context import ExecutionClaims, TestRef
from tests.e2e.core.observability.writer import (
    JournalWriter,
    RunDirs,
    bounded_text,
    monotonic_ns,
    new_invocation_id,
    open_run,
    read_collection_sidecar,
    safe_hash,
    safe_test_id,
    write_collection_sidecar,
)

OUTPUT_OPTION = "--ci-observability-output"
CONTEXT_OPTION = "--ci-observability-context"
OUTPUT_ENV = "OSAC_CI_OBS_OUTPUT"
CONTEXT_ENV = "OSAC_CI_OBS_CONTEXT"
INVOCATION_KEY = "ci_obs_invocation_id"
INVOCATION_ENV = "OSAC_CI_OBS_INVOCATION_ID"

_FAILURE_OUTCOMES = frozenset({"failed", "error", "timeout"})
_ORIGINAL_FINISH: object | None = None
_WRAP_USERS = 0

_tracker: Tracker | None = None
# Nested in-process runs: inner config id -> suspended outer tracker. A
# per-config map keeps three-level nesting correct where a single global
# suspend slot would restore the wrong tracker.
_suspended: dict[int, Tracker] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-observability")
    group.addoption(
        "--ci-observability-output",
        dest="ci_observability_output",
        action="store",
        default=None,
        metavar="DIR",
        help="Private output directory for CI observability journals (requires --ci-observability-context).",
    )
    group.addoption(
        "--ci-observability-context",
        dest="ci_observability_context",
        action="store",
        default=None,
        metavar="PATH",
        help="Trusted CI observability context document (requires --ci-observability-output).",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _tracker
    if _tracker is not None and _tracker.owner_config is config:
        return
    output = config.getoption("ci_observability_output") or os.environ.get(OUTPUT_ENV)
    context_path = config.getoption("ci_observability_context") or os.environ.get(CONTEXT_ENV)
    if _tracker is not None:
        # A nested in-process pytest run (pytest.main inside a test). Its
        # records must never join the outer invocation, and the outer
        # tracker, journal, context bindings, and class wrapper must
        # survive it. Suspend the outer tracker for this config; the
        # inner run either activates its own tracker below (when fully
        # configured) or runs unrecorded with an explicit gap in the
        # outer journal.
        outer = _tracker
        _suspended[id(config)] = outer
        _tracker = None
        obs_context._bind_state(journal=None, operations=None)
        if not output or not context_path:
            outer.note_nested_run()
            _warn(message="nested pytest run is not configured to record; leaving a collection gap")
            return
    if not output and not context_path:
        return
    if not output or not context_path:
        _warn(message="both an output directory and a context document are required; plugin inactive")
        return
    try:
        claims = ExecutionClaims.from_json_path(path=Path(context_path))
    except (OSError, ValueError) as error:
        _warn(message=f"context unusable ({error}); plugin inactive")
        return
    try:
        run_root = Path(output).resolve()
    except (OSError, RuntimeError, ValueError):
        _warn(message="output path could not be resolved; plugin inactive")
        return
    _tracker = Tracker(config=config, claims=claims, run_root=run_root)
    _tracker.activate()


def pytest_unconfigure(config: pytest.Config) -> None:
    global _tracker
    # An inner run that activated its own tracker finishes first; the
    # suspended outer tracker is restored afterwards, untouched.
    if _tracker is not None and _tracker.owner_config is config:
        tracker = _tracker
        _tracker = None
        tracker.shutdown()
    outer = _suspended.pop(id(config), None)
    if outer is not None:
        _tracker = outer
        outer.bind_context()


def _warn(*, message: str) -> None:
    with contextlib.suppress(OSError):
        print(f"ci-observability: {message}", file=sys.stderr, flush=True)


def _filter_identity(*, expression: str) -> str:
    """Filter expressions can quote parameter values verbatim, so only the hash is recorded."""
    return f"p:{safe_hash(text=expression)[:12]}"


def _namespace_digest(*, baseid: str) -> str:
    """Root is one scope; any other baseid is digested whole with its raw

    separator, so ``parent.child`` and ``parent/child`` stay distinct.
    """
    normalized = baseid.strip("/")
    if not normalized or normalized == ".":
        return ""
    return safe_hash(text=normalized)[:12]


class FixtureInstance:
    """Identity of one fixture setup-to-teardown lifetime."""

    def __init__(
        self, *, instance_id: str, fixture_id: str, scope: str, worker_id: str, params_fingerprint: str | None
    ) -> None:
        self.instance_id = instance_id
        self.fixture_id = fixture_id
        self.scope = scope
        self.worker_id = worker_id
        self.params_fingerprint = params_fingerprint


class Tracker:
    """Per-process producer state; one per controller, worker, or serial main."""

    def __init__(self, *, config: pytest.Config, claims: ExecutionClaims, run_root: Path) -> None:
        self.owner_config = config
        self.config = config
        self.claims = claims
        self.run_root = run_root
        self.run_dirs: RunDirs = open_run(run_root=run_root)
        if self.run_dirs.journals is None:
            # Nothing can be persisted in the bundle; say so on stderr once
            # so the gap is visible even where no status file can be written.
            problems = ", ".join(self.run_dirs.problems) or "unknown"
            _warn(message=f"journals unavailable ({problems}); no records will be written")
        # Worker identity comes only from xdist's config.workerinput: the
        # PYTEST_XDIST_WORKER variable is inherited by every subprocess a
        # worker spawns, so a serial pytest child started inside a worker
        # would otherwise be misclassified as that worker.
        workerinput = getattr(config, "workerinput", None)
        self.is_worker = workerinput is not None
        self.worker_id = "main"
        self.worker_id_known = True
        if self.is_worker:
            workerid = (workerinput or {}).get("workerid")
            if isinstance(workerid, str) and workerid:
                self.worker_id = workerid
            else:
                # A real worker without a usable id never gets a
                # fabricated serial-main identity: records carry the
                # explicit unknown marker and the gap is counted below.
                self.worker_id = "unknown"
                self.worker_id_known = False
        numprocesses = getattr(config.option, "numprocesses", None)
        self.is_parallel_controller = bool(numprocesses) and not self.is_worker
        self.invocation_id = claims.invocation_id or new_invocation_id()
        self.invocation_id_known = True
        if self.is_worker:
            inherited = (workerinput or {}).get(INVOCATION_KEY) or os.environ.get(INVOCATION_ENV)
            if inherited:
                self.invocation_id = str(inherited)
            else:
                # The controller's invocation id never reached this worker.
                # Records could not join the invocation, and fabricating a
                # fresh id would produce an invalid bundle, so the writer
                # records the gap and stays silent.
                self.invocation_id_known = False
        self.writer = JournalWriter(
            run_dirs=self.run_dirs,
            journal_id=self.invocation_id if not self.is_worker else f"{self.invocation_id}-{self.worker_id}",
            execution_id=claims.execution_id,
            invocation_id=self.invocation_id if self.invocation_id_known else None,
        )
        self.invocation_start_mono = monotonic_ns()
        self.exit_status: int | None = None
        self.attempts: dict[str, int] = {}
        self.current: tuple[str, int] | None = None
        self.open_phases: dict[tuple[str, int, str], tuple[int, int | None]] = {}
        self.first_failure: dict[str, object] | None = None
        self.collection_error = False
        self.collection_emitted = False
        self.collection: tuple[list[str], list[tuple[str, str | None]]] | None = None
        self.fixture_registry: dict[int, FixtureInstance] = {}
        # Setup ordinals keyed by the canonical definition/parameter
        # identity, not by id(fixturedef): pytest builds a fresh FixtureDef
        # object per parametrize mark, so an object-keyed counter sees
        # "first setup" repeatedly for the same identity and distinct
        # lifetimes would collide on one instance id. Counting per
        # identity gives every actual setup (a real cache miss) the next
        # ordinal, while one cached definition still holds ordinal 1 for
        # its single setup.
        self.fixture_setup_ordinals: dict[str, int] = {}
        self.operation_ids: set[str] = set()

    def activate(self) -> None:
        if not self.invocation_id_known:
            self.writer.dropped_records += 1
            self.writer.errors.append("invocation-id-unavailable")
            return
        if not self.worker_id_known:
            # A real worker whose workerinput lacks a usable id records
            # with the explicit "unknown" marker, never a fake "main";
            # the counted gap reaches the writer status file and the
            # manifest's completeness errors.
            self.writer.dropped_records += 1
            self.writer.errors.append("worker-id-unavailable")
        if not _install_finish_wrap():
            # The private pytest internals are unavailable on this version,
            # so fixture finalizer cost cannot be measured: a counted,
            # visible gap instead of a silent loss.
            self.writer.dropped_records += 1
            self.writer.errors.append("fixture-finish-wrap-unavailable")
        if not self.is_worker:
            self.writer.emit(event="invocation_started", data=self._invocation_started_data())
        self.bind_context()

    def bind_context(self) -> None:
        """Point the W08 operation API at this tracker's journal and test."""
        if not self.is_worker:
            # Restored after a nested run overwrote it for its own workers.
            os.environ[INVOCATION_ENV] = self.invocation_id
        obs_context._bind_state(journal=self.writer, operations=self.operation_ids)
        if self.current is not None:
            nodeid, attempt = self.current
            obs_context._set_current_test(
                ref=TestRef(test_id=safe_test_id(test_id=nodeid), attempt_index=attempt, worker_id=self.worker_id)
            )
        else:
            obs_context._set_current_test(ref=None)

    def note_nested_run(self) -> None:
        """Leave an explicit gap: a nested in-process run went unrecorded."""
        self.writer.dropped_records += 1
        self.writer.errors.append("nested-invocation-unrecorded")
        self.writer.emit(
            event="collection_state",
            data={
                "source": "pytest-nested-invocation",
                "status": "unavailable",
                "complete": False,
                "dropped_records": 1,
                "errors": [{"message": "nested-invocation-unrecorded"}],
            },
        )

    def _invocation_started_data(self) -> dict[str, object]:
        suite = self.claims.suite_payload()
        data: dict[str, object] = {
            "invocation_id": self.invocation_id,
            "framework": "pytest",
            "tier": suite["tier"],
            "component": suite["component"],
            "variant": suite["variant"],
            "selection": self._selection_payload(),
        }
        if self.claims.dependencies is not None:
            data["dependencies"] = list(self.claims.dependencies)
        return data

    def _selection_payload(self) -> dict[str, object]:
        selection: dict[str, object] = {"worker_count": self._worker_count()}
        paths = [
            bounded_text(text=safe_test_id(test_id=str(argument)), limit=256) for argument in (self.config.args or [])
        ][:32]
        if paths:
            selection["paths"] = paths
        markexpr = getattr(self.config.option, "markexpr", None)
        keyword = getattr(self.config.option, "keyword", None)
        if markexpr:
            selection["markers"] = _filter_identity(expression=markexpr)
        if keyword:
            selection["keyword"] = _filter_identity(expression=keyword)
        return selection

    def _worker_count(self) -> int:
        numprocesses = getattr(self.config.option, "numprocesses", None)
        if isinstance(numprocesses, int) and numprocesses > 0:
            return numprocesses
        return 0

    def shutdown(self) -> None:
        source = f"pytest-journal-{self.writer.journal_id}"
        self.writer.emit_terminal_state(source=source)
        if self.is_worker:
            outcome = _worker_stop_outcome(status=self.exit_status)
            self.writer.emit(event="worker_stopped", data={"worker_id": self.worker_id, "outcome": outcome})
        else:
            data: dict[str, object] = {
                "invocation_id": self.invocation_id,
                "outcome": _exit_outcome(status=self.exit_status),
                "exit_code": self.exit_status,
                "duration_ns": monotonic_ns() - self.invocation_start_mono,
            }
            if self.first_failure is not None:
                data["failure"] = self.first_failure
            self.writer.emit(event="invocation_finished", data=data)
        status = "stopped" if self.writer.dropped_records == 0 and not self.writer.errors else "degraded"
        self.writer.close(status=status)
        obs_context._bind_state(journal=None, operations=None)
        obs_context._set_current_test(ref=None)
        _remove_finish_wrap()

    def deselection_reason(self) -> str | None:
        markexpr = getattr(self.config.option, "markexpr", None)
        if markexpr:
            return f"marker filter: {_filter_identity(expression=markexpr)}"
        keyword = getattr(self.config.option, "keyword", None)
        if keyword:
            return f"keyword filter: {_filter_identity(expression=keyword)}"
        return None

    def record_collection(self, *, collected: list[str], deselected: list[tuple[str, str | None]]) -> None:
        if self.is_worker:
            # The sidecar is producer output too: parameter values are
            # replaced by safe hashes before it is written.
            payload = {
                "journal_id": self.writer.journal_id,
                "worker_id": self.worker_id,
                "collected": [safe_test_id(test_id=test_id) for test_id in collected],
                "deselected": [
                    {"test_id": safe_test_id(test_id=test_id), **({"deselection_reason": reason} if reason else {})}
                    for test_id, reason in deselected
                ],
                "error": self.collection_error,
            }
            if not write_collection_sidecar(
                state_dir=self.run_dirs.state, journal_id=self.writer.journal_id, payload=payload
            ):
                # The sidecar is how the controller learns this worker's
                # collection; a failed write is a counted, visible gap.
                self.writer.dropped_records += 1
                self.writer.errors.append("collection-sidecar-write-failed")
        elif not self.is_parallel_controller:
            self.collection = (collected, deselected)

    def emit_collection_records(self) -> None:
        """Serial main: emit the invocation's collection definition."""
        if self.collection_emitted:
            return
        self.collection_emitted = True
        if self.collection_error:
            self.writer.emit(event="collection_finished", data={"outcome": "error"})
            return
        collected, deselected = self.collection or ([], [])
        deselected_ids = {test_id for test_id, _reason in deselected}
        for test_id in collected:
            data: dict[str, object] = {
                "test_id": safe_test_id(test_id=test_id),
                "deselected": test_id in deselected_ids,
            }
            reason = next((r for tid, r in deselected if tid == test_id), None)
            if reason:
                data["deselection_reason"] = reason
            self.writer.emit(event="test_collected", data=data)
        self.writer.emit(
            event="collection_finished",
            data={"outcome": "completed", "collected_count": len(collected), "deselected_count": len(deselected)},
        )

    def emit_xdist_collection(self, *, node: object, ids: Sequence[str]) -> None:
        """Controller: one collection definition from the first worker's real data."""
        if self.collection_emitted:
            return
        self.collection_emitted = True
        if self.collection_error:
            self.writer.emit(event="collection_finished", data={"outcome": "error"})
            return
        worker_journal = f"{self.invocation_id}-{node.gateway.id}"
        sidecar = read_collection_sidecar(state_dir=self.run_dirs.state, journal_id=worker_journal)
        if sidecar is None:
            self.writer.errors.append("worker-collection-metadata-missing")
            self.writer.dropped_records += 1
            self.writer.emit(event="collection_finished", data={"outcome": "unknown"})
            return
        collected = [str(test_id) for test_id in sidecar.get("collected", [])]
        deselected = [dict(entry) for entry in sidecar.get("deselected", [])]
        deselected_ids = {str(entry["test_id"]) for entry in deselected}
        # The worker-reported ids are raw node ids; the sidecar and every
        # emitted record carry the safe form, so compare safe to safe.
        reported = {safe_test_id(test_id=test_id) for test_id in ids}
        if reported != set(collected) - deselected_ids:
            self.writer.errors.append("worker-collection-metadata-mismatch")
            self.writer.dropped_records += 1
            self.writer.emit(event="collection_finished", data={"outcome": "unknown"})
            return
        # The sidecar ids are already safe; hashing them again would
        # desynchronize collected ids from the phase records.
        for test_id in collected:
            data: dict[str, object] = {"test_id": test_id, "deselected": test_id in deselected_ids}
            reason = next(
                (str(entry.get("deselection_reason")) for entry in deselected if entry.get("test_id") == test_id), None
            )
            if reason:
                data["deselection_reason"] = reason
            self.writer.emit(event="test_collected", data=data)
        self.writer.emit(
            event="collection_finished",
            data={"outcome": "completed", "collected_count": len(collected), "deselected_count": len(deselected)},
        )

    def begin_attempt(self, *, nodeid: str) -> int:
        count = self.attempts.get(nodeid, 0) + 1
        self.attempts[nodeid] = count
        self.current = (nodeid, count)
        obs_context._set_current_test(
            ref=TestRef(test_id=safe_test_id(test_id=nodeid), attempt_index=count, worker_id=self.worker_id)
        )
        return count

    def end_attempt(self) -> None:
        self.current = None
        obs_context._set_current_test(ref=None)

    def phase_started(self, *, nodeid: str, attempt: int, phase: str) -> None:
        key = (nodeid, attempt, phase)
        self.open_phases[key] = (monotonic_ns(), None)
        self.writer.emit(
            event="test_phase_started",
            data={
                "test_id": safe_test_id(test_id=nodeid),
                "attempt_index": attempt,
                "phase": phase,
                "worker_id": self.worker_id,
            },
        )

    def phase_ended(self, *, nodeid: str, attempt: int, phase: str, end_mono: int) -> None:
        """Record when the phase's actual work ended, at the phase boundary.

        Report generation and diagnostic hooks run after the boundary; the
        recorded duration must charge only the work, so the end time is
        captured when the phase hook chain returns, not when the record is
        emitted.
        """
        key = (nodeid, attempt, phase)
        span = self.open_phases.get(key)
        if span is not None:
            self.open_phases[key] = (span[0], end_mono)

    def phase_finished(
        self, *, nodeid: str, attempt: int, phase: str, outcome: str, failure: dict[str, object] | None = None
    ) -> None:
        key = (nodeid, attempt, phase)
        span = self.open_phases.pop(key, None)
        if span is None:
            return
        started, ended = span
        if failure is not None and self.first_failure is None and outcome in _FAILURE_OUTCOMES:
            self.first_failure = {**failure, "phase": phase}
        data: dict[str, object] = {
            "test_id": safe_test_id(test_id=nodeid),
            "attempt_index": attempt,
            "phase": phase,
            "worker_id": self.worker_id,
            "outcome": outcome,
        }
        if failure is not None and outcome in _FAILURE_OUTCOMES:
            data["failure"] = failure
        data["duration_ns"] = (ended if ended is not None else monotonic_ns()) - started
        self.writer.emit(event="test_phase_finished", data=data)

    def handle_report(self, *, item: pytest.Item, call: pytest.CallInfo[object], report: pytest.TestReport) -> None:
        if self.current is None or report.when not in ("setup", "call", "teardown"):
            return
        nodeid, attempt = self.current
        key = (nodeid, attempt, report.when)
        if key not in self.open_phases:
            return
        outcome, failure = _report_outcome(item=item, call=call, report=report)
        self.phase_finished(nodeid=nodeid, attempt=attempt, phase=report.when, outcome=outcome, failure=failure)
        if report.when == "teardown":
            self.emit_use_edges(item=item, nodeid=nodeid, attempt=attempt)

    def emit_use_edges(self, *, item: pytest.Item, nodeid: str, attempt: int) -> None:
        request = getattr(item, "_request", None)
        fixture_defs = getattr(request, "_fixture_defs", None) if request else None
        if not fixture_defs:
            return
        seen: set[str] = set()
        for fixturedef in list(fixture_defs.values()):
            instance = self.fixture_registry.get(id(fixturedef))
            if instance is None or instance.instance_id in seen:
                continue
            seen.add(instance.instance_id)
            self.writer.emit(
                event="fixture_used",
                data={
                    "fixture_instance_id": instance.instance_id,
                    "test_id": safe_test_id(test_id=nodeid),
                    "attempt_index": attempt,
                    "worker_id": self.worker_id,
                },
            )

    def begin_fixture_setup(self, *, fixturedef: object, request: pytest.FixtureRequest) -> tuple[FixtureInstance, int]:
        registry_key = id(fixturedef)
        fingerprint = self._params_fingerprint(fixturedef=fixturedef, request=request)
        # Definition identity derives from the normalized raw root-relative
        # FixtureDef.baseid (as a digest), the module, and the qualified
        # name. A parent and a child conftest can define the same argname
        # with the same qualname (both are module "conftest"), and a
        # directory named parent.child must not collide with parent/child:
        # the raw separator survives in the digest input. One definition
        # reused through its cache keeps one id; a root-level definition
        # (empty or "." baseid) stays a legitimate unprefixed identity.
        namespace_tag = _namespace_digest(baseid=fixturedef.baseid)
        function = getattr(fixturedef, "func", None)
        module = getattr(function, "__module__", None) or "unknown"
        qualname = getattr(function, "__qualname__", None) or fixturedef.argname
        if namespace_tag:
            fixture_id = bounded_text(text=f"ns{namespace_tag}.{module}.{qualname}", limit=256)
        else:
            fixture_id = bounded_text(text=f"{module}.{qualname}", limit=256)
        identity = f"{namespace_tag}|{module}|{qualname}|{fingerprint or ''}"
        # The ordinal is the count of actual setups of this identity in
        # this process, so a fresh per-mark FixtureDef with an already
        # seen identity still gets its own lifetime, while a cached
        # definition's single setup keeps the first ordinal.
        ordinal = self.fixture_setup_ordinals.get(identity, 0) + 1
        self.fixture_setup_ordinals[identity] = ordinal
        instance_id = "fx-" + safe_hash(text=f"{identity}|{self.worker_id}|{ordinal}")[:16]
        instance = FixtureInstance(
            instance_id=instance_id,
            fixture_id=fixture_id,
            scope=fixturedef.scope,
            worker_id=self.worker_id,
            params_fingerprint=fingerprint,
        )
        self.fixture_registry[registry_key] = instance
        data: dict[str, object] = {
            "fixture_instance_id": instance.instance_id,
            "fixture_id": instance.fixture_id,
            "scope": instance.scope,
            "worker_id": instance.worker_id,
            "phase": "setup",
        }
        if fingerprint is not None:
            data["params_fingerprint"] = fingerprint
        self.writer.emit(event="fixture_started", data=data)
        return instance, monotonic_ns()

    def end_fixture_setup(
        self, *, instance: FixtureInstance, started_mono: int, excinfo: tuple[object, BaseException, object] | None
    ) -> None:
        outcome = "passed"
        failure = None
        if excinfo is not None:
            exception = excinfo[1]
            if isinstance(exception, _xfail_exception()):
                outcome = "expected_failure"
            elif isinstance(exception, _skipped_exception()):
                outcome = "skipped"
            else:
                outcome = "error"
                failure = {"signature": _safe_exception_name(error=exception)}
        data: dict[str, object] = {
            "fixture_instance_id": instance.instance_id,
            "fixture_id": instance.fixture_id,
            "scope": instance.scope,
            "worker_id": instance.worker_id,
            "phase": "setup",
            "outcome": outcome,
        }
        if failure is not None:
            data["failure"] = failure
        data["duration_ns"] = monotonic_ns() - started_mono
        self.writer.emit(event="fixture_finished", data=data)

    def fixture_finish(self, *, fixturedef: object, request: object, original: Callable[..., object]) -> object:
        instance = self.fixture_registry.get(id(fixturedef))
        if instance is None or fixturedef.cached_result is None:
            return original(fixturedef, request)
        data: dict[str, object] = {
            "fixture_instance_id": instance.instance_id,
            "fixture_id": instance.fixture_id,
            "scope": instance.scope,
            "worker_id": instance.worker_id,
            "phase": "teardown",
        }
        if instance.params_fingerprint is not None:
            data["params_fingerprint"] = instance.params_fingerprint
        self.writer.emit(event="fixture_started", data=data)
        started = monotonic_ns()
        try:
            result = original(fixturedef, request)
        except BaseException as error:
            self._emit_fixture_teardown(instance=instance, started_mono=started, error=error)
            raise
        self._emit_fixture_teardown(instance=instance, started_mono=started, error=None)
        return result

    def _emit_fixture_teardown(
        self, *, instance: FixtureInstance, started_mono: int, error: BaseException | None
    ) -> None:
        outcome = "passed"
        failure = None
        if error is not None:
            outcome = "error"
            failure = {"signature": _safe_exception_name(error=error)}
        data: dict[str, object] = {
            "fixture_instance_id": instance.instance_id,
            "fixture_id": instance.fixture_id,
            "scope": instance.scope,
            "worker_id": instance.worker_id,
            "phase": "teardown",
            "outcome": outcome,
        }
        if failure is not None:
            data["failure"] = failure
        data["duration_ns"] = monotonic_ns() - started_mono
        self.writer.emit(event="fixture_finished", data=data)

    def _params_fingerprint(self, *, fixturedef: object, request: pytest.FixtureRequest) -> str | None:
        if not hasattr(request, "param"):
            return None
        index = getattr(request, "param_index", 0)
        params = getattr(fixturedef, "params", None)
        count = len(params) if params is not None else -1
        return safe_hash(text=f"{fixturedef.argname}|{index}|{count}")


def _skipped_exception() -> type[BaseException]:
    skipped = getattr(pytest, "Skipped", None)
    if skipped is None:
        skipped = pytest.skip.Exception
    return skipped


def _xfail_exception() -> type[BaseException]:
    return pytest.xfail.Exception


def _safe_exception_name(*, error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        names = sorted({type(member).__name__ for member in error.exceptions})
        return bounded_text(text="|".join(names) or "ExceptionGroup", limit=512)
    return bounded_text(text=type(error).__name__, limit=512)


def _exit_outcome(*, status: int | None) -> str:
    if status is None:
        return "unknown"
    if status == 0:
        return "passed"
    if status == 2:
        return "cancelled"
    return "failed"


def _worker_stop_outcome(*, status: int | None) -> str:
    # A worker that reaches its own sessionfinish completed its work even
    # when its tests failed: the exit status carries test results, not
    # worker health. Only an interrupt means the worker itself stopped.
    if status == 2:
        return "interrupted"
    return "completed"


def _report_outcome(
    *, item: pytest.Item, call: pytest.CallInfo[object], report: pytest.TestReport
) -> tuple[str, dict[str, object] | None]:
    wasxfail = hasattr(report, "wasxfail")
    if report.outcome == "skipped":
        return ("expected_failure" if wasxfail else "skipped"), None
    if report.outcome == "passed":
        return ("unexpected_pass" if wasxfail else "passed"), None
    if wasxfail:
        return "unexpected_pass", None
    outcome = "failed" if report.when == "call" else "error"
    # A strict expected failure that unexpectedly passes is reported by
    # pytest as a plain failure, without wasxfail; the evaluated xfail
    # state on the item is the native evidence that distinguishes it from
    # an ordinary failure. Only the call phase with no exception can be
    # an unexpected pass; a real failure under an xfail mark stays failed.
    if outcome == "failed" and call.excinfo is None and _strict_xfail_applied(item=item):
        return "unexpected_pass", None
    if call.excinfo is not None:
        return outcome, {"signature": _safe_exception_name(error=call.excinfo.value)}
    return outcome, {"signature": "unrepresented-failure"}


def _strict_xfail_applied(*, item: pytest.Item) -> bool:
    """Uses the evaluator state pytest computed, never the marker's presence;

    False keeps the native outcome when the private signal is unavailable.
    """
    if xfailed_key is None:
        return False
    xfailed = item.stash.get(xfailed_key, None)
    return xfailed is not None and bool(xfailed.strict)


def _install_finish_wrap() -> bool:
    """Class-level wrap, reference-counted so nested runs cannot remove it

    under an outer tracker. False when this pytest version lacks the private API.
    """
    global _ORIGINAL_FINISH, _WRAP_USERS
    if FixtureDef is None:
        return False
    if _WRAP_USERS == 0:
        _ORIGINAL_FINISH = FixtureDef.finish

        original = FixtureDef.finish

        def finish(self: object, request: object) -> object:
            tracker = _tracker
            # The class wrapper is shared by every pytest run in this
            # process, including nested runs that never loaded this
            # plugin: only fixtures that belong to the tracker's own
            # config are recorded.
            if tracker is None or getattr(request, "config", None) is not tracker.owner_config:
                return original(self, request)
            return tracker.fixture_finish(fixturedef=self, request=request, original=original)

        FixtureDef.finish = finish
    _WRAP_USERS += 1
    return True


def _remove_finish_wrap() -> None:
    global _ORIGINAL_FINISH, _WRAP_USERS
    if _WRAP_USERS == 0:
        return
    _WRAP_USERS -= 1
    if _WRAP_USERS > 0 or FixtureDef is None:
        return
    FixtureDef.finish = _ORIGINAL_FINISH
    _ORIGINAL_FINISH = None


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: object) -> None:
    """xdist controller hook: hand the invocation id to the worker.

    Optional: the hook specification only exists when pytest-xdist is
    loaded, so the plugin stays usable without it.
    """
    tracker = _tracker
    if tracker is not None and not tracker.is_worker:
        node.workerinput[INVOCATION_KEY] = tracker.invocation_id


def pytest_sessionstart(session: pytest.Session) -> None:
    tracker = _tracker
    if tracker is None or not tracker.is_worker:
        return
    tracker.writer.emit(
        event="worker_started",
        data={
            "worker_id": tracker.worker_id,
            "host": bounded_text(text=socket.gethostname(), limit=128),
            "pid": os.getpid(),
        },
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    tracker = _tracker
    if tracker is not None:
        tracker.exit_status = int(exitstatus)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object) -> None:
    """xdist controller hook: record worker loss the controller observed."""
    tracker = _tracker
    if tracker is None or tracker.is_worker or error is None:
        return
    tracker.writer.emit(event="worker_stopped", data={"worker_id": node.gateway.id, "outcome": "interrupted"})


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: object, ids: Sequence[str]) -> None:
    tracker = _tracker
    if tracker is None or tracker.is_worker:
        return
    tracker.emit_xdist_collection(node=node, ids=ids)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    tracker = _tracker
    if tracker is not None and getattr(report, "failed", False):
        tracker.collection_error = True


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Iterator[object]:
    tracker = _tracker
    if tracker is None:
        yield
        return
    before = list(items)
    before_ids = {id(item) for item in before}
    outcome = yield
    del outcome
    remaining = {id(item) for item in items}
    # Hooks may add items; those items run, so they belong to the collected
    # set and its counts. Deselected items are those removed from the list.
    collected_items = before + [item for item in items if id(item) not in before_ids]
    collected = [item.nodeid for item in collected_items]
    reason = tracker.deselection_reason()
    deselected = [(item.nodeid, reason) for item in before if id(item) not in remaining]
    tracker.record_collection(collected=collected, deselected=deselected)


def pytest_collection_finish(session: pytest.Session) -> None:
    tracker = _tracker
    if tracker is None or tracker.is_worker or tracker.is_parallel_controller:
        return
    tracker.emit_collection_records()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[object]:
    tracker = _tracker
    if tracker is None:
        yield
        return
    tracker.begin_attempt(nodeid=item.nodeid)
    try:
        outcome = yield
        del outcome
    finally:
        tracker.end_attempt()


def _phase_hook(*, item: pytest.Item, phase: str) -> Iterator[object]:
    tracker = _tracker
    if tracker is None or tracker.current is None:
        yield
        return
    nodeid, attempt = tracker.current
    tracker.phase_started(nodeid=nodeid, attempt=attempt, phase=phase)
    outcome = yield
    # The phase's work ends when its hook chain returns: capture that
    # boundary now, before report generation and diagnostic hooks run, so
    # their latency is never charged to the phase. The report is still the
    # authority for the outcome; only an interrupt, which stops the
    # protocol before any report exists, is recorded here.
    end_mono = monotonic_ns()
    tracker.phase_ended(nodeid=nodeid, attempt=attempt, phase=phase, end_mono=end_mono)
    if outcome.excinfo is not None and isinstance(outcome.excinfo[1], KeyboardInterrupt):
        tracker.phase_finished(nodeid=nodeid, attempt=attempt, phase=phase, outcome="interrupted", failure=None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[object]:
    yield from _phase_hook(item=item, phase="setup")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[object]:
    yield from _phase_hook(item=item, phase="call")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[object]:
    yield from _phase_hook(item=item, phase="teardown")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]) -> Iterator[object]:
    tracker = _tracker
    if tracker is None:
        yield
        return
    outcome = yield
    tracker.handle_report(item=item, call=call, report=outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: object, request: pytest.FixtureRequest) -> Iterator[object]:
    tracker = _tracker
    if tracker is None or fixturedef.argname == "request":
        yield
        return
    instance, started = tracker.begin_fixture_setup(fixturedef=fixturedef, request=request)
    outcome = yield
    tracker.end_fixture_setup(instance=instance, started_mono=started, excinfo=outcome.excinfo)
