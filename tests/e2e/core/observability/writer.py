"""Private journal writer and bundle finalizer for CI observability producers.

On-disk shape of one producer run:

    <run root>/bundle/journals/<journal_id>.jsonl   append-only JSON Lines
    <run root>/bundle/native-report/<name>          native reports declared by the footer
    <run root>/state/                               transient producer state

The run root, bundle, journals and native-report directories are mode 0700
and every journal file is mode 0600, so journals stay private to the runner
account until the approved redaction and delivery path (W06) runs. Each
process writes its own journal and never appends to another process's
journal.

The job-level header and footer (``execution_started`` and
``execution_finished`` records plus the bundle manifest) are owned by the
trusted workflow bootstrap, which calls :func:`write_job_header` and
:meth:`JobJournal.finish`. The pytest plugin only writes invocation,
worker, test, fixture, and operation journals. W04 wires the bootstrap into
the workflows and must write the header before installation starts.

Every write failure is counted and surfaced: dropped-record counts,
fixed-label error notes, and per-writer status files under ``state/``. A
write failure never raises into the framework being measured, and error
text never carries producer-controlled strings beyond fixed labels and
errno codes.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "osac.ci-observability.journal-record"
MANIFEST_SCHEMA = "osac.ci-observability.bundle-manifest"
SCHEMA_VERSION = 1
MANIFEST_NAME = "bundle-manifest.json"
PRODUCER_NAME = "osac-ci-observability-pytest"
PRODUCER_VERSION = "0.1.0"

MAX_ERROR_NOTES = 32
MAX_ERROR_CHARS = 512
MAX_TEST_ID = 512
MAX_NATIVE_REPORT_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 1 << 20

# A journal is complete when its last flushed record is its natural
# terminal event: an invocation journal ends with invocation_finished, a
# worker journal with worker_stopped, and the job journal with
# execution_finished. Anything else (dangling starts, truncated lines)
# means the writer never reached its terminal record.
_TERMINAL_EVENTS = frozenset({"invocation_finished", "worker_stopped", "execution_finished"})


def utc_now() -> str:
    """UTC wall-clock timestamp in the contract's format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_invocation_id() -> str:
    """A fresh invocation id, filename-safe and unique per process."""
    return f"inv-{uuid.uuid4().hex[:16]}"


def monotonic_ns() -> int:
    return time.monotonic_ns()


def safe_hash(*, text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def bounded_test_id(*, test_id: str) -> str:
    """A contract-length test id; overlong ids are truncated with a hash suffix."""
    if len(test_id) <= MAX_TEST_ID:
        return test_id
    digest = safe_hash(text=test_id)[:31]
    return f"{test_id[: MAX_TEST_ID - 32]}-{digest}"


def safe_test_id(*, test_id: str) -> str:
    """The payload opened by the first ``[`` after the first ``::`` is hashed

    whole into ``[p:<12 hex>]``; first-match wins because both separators
    can appear inside a parameter value. The README carries the full policy.
    """
    path, separator, name = test_id.partition("::")
    if not separator:
        return bounded_test_id(test_id=test_id)
    opening = name.find("[")
    if opening == -1:
        return bounded_test_id(test_id=test_id)
    payload = name[opening + 1 :]
    if payload.endswith("]"):
        payload = payload[:-1]
    digest = safe_hash(text=payload)[:12]
    return bounded_test_id(test_id=f"{path}{separator}{name[:opening]}[p:{digest}]")


def bounded_text(*, text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


@dataclass(frozen=True)
class RunDirs:
    """Validated directories of one producer run, or None where unusable.

    ``problems`` carries fixed-label notes for every directory that could
    not be created or validated (for example a symlinked journals path).
    """

    journals: Path | None
    state: Path | None
    problems: tuple[str, ...] = ()


def _ensure_private_dir(*, path: Path) -> None:
    """Create one private directory, refusing to write through symlinks."""
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.EEXIST, "not a directory", str(path))
    os.chmod(path, 0o700)


def open_run(*, run_root: Path) -> RunDirs:
    """Open (creating if needed) the run root and its subdirectories.

    Each subdirectory is validated independently, so an unusable journals
    directory still leaves the state directory available for failure
    markers: a collection gap stays visible even when journals cannot be
    written at all.
    """
    problems: list[str] = []
    try:
        _ensure_private_dir(path=run_root)
    except OSError as error:
        return RunDirs(journals=None, state=None, problems=(f"run-root-unusable-{_errno_code(error=error)}",))

    journals: Path | None = None
    state: Path | None = None
    try:
        _ensure_private_dir(path=run_root / "bundle")
        _ensure_private_dir(path=run_root / "bundle" / "journals")
        journals = run_root / "bundle" / "journals"
    except OSError as error:
        problems.append(f"journals-dir-unusable-{_errno_code(error=error)}")
    try:
        _ensure_private_dir(path=run_root / "state")
        state = run_root / "state"
    except OSError as error:
        problems.append(f"state-dir-unusable-{_errno_code(error=error)}")
    return RunDirs(journals=journals, state=state, problems=tuple(problems))


def _errno_code(*, error: OSError) -> str:
    return errno.errorcode.get(error.errno, "EIO").lower()


class JournalWriter:
    """Never raises into the measured framework: failures become counted drops

    with fixed labels. Sequences advance only when bytes reach the file, and
    writes go through the raw descriptor so a dropped line cannot be
    resurrected out of order.
    """

    def __init__(
        self, *, run_dirs: RunDirs, journal_id: str, execution_id: str, invocation_id: str | None = None
    ) -> None:
        self.execution_id = execution_id
        self.invocation_id = invocation_id
        self.dropped_records = 0
        self.errors: list[str] = []
        self.started_at = utc_now()
        self.journal_id = journal_id
        self._sequence = 0
        self._bytes = 0
        self._closed = False
        self._descriptor: int | None = None
        self._run_dirs = run_dirs
        for problem in run_dirs.problems:
            self._note_error(message=problem)
        if run_dirs.journals is None:
            return
        try:
            path, descriptor = self._create_journal_file(journals_dir=run_dirs.journals, journal_id=journal_id)
        except OSError as error:
            self._note_error(message=f"journal-open-failed-{_errno_code(error=error)}")
            return
        self.journal_id = path.stem
        self._descriptor = descriptor

    @staticmethod
    def _create_journal_file(*, journals_dir: Path, journal_id: str) -> tuple[Path, int]:
        """O_EXCL: a planted symlink or existing name is never written through."""
        for attempt in range(1, 100):
            suffix = "" if attempt == 1 else f"-{attempt}"
            candidate = journals_dir / f"{journal_id}{suffix}.jsonl"
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                continue
            return candidate, descriptor
        raise OSError(errno.EEXIST, "journal name exhausted", journal_id)

    @property
    def usable(self) -> bool:
        return self._descriptor is not None and not self._closed

    @property
    def record_count(self) -> int:
        return self._sequence

    def _note_error(self, *, message: str) -> None:
        # Notes stay unique and bounded: the count of dropped records
        # carries the volume, the notes carry the distinct causes.
        message = bounded_text(text=message, limit=MAX_ERROR_CHARS)
        if message not in self.errors and len(self.errors) < MAX_ERROR_NOTES:
            self.errors.append(message)

    def _drop(self, *, label: str) -> None:
        self.dropped_records += 1
        self._note_error(message=label)

    def emit(self, *, event: str, data: Mapping[str, object]) -> bool:
        """Append one record; False when the record could not be persisted."""
        if self._closed:
            return False
        record: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "journal_id": self.journal_id,
            "sequence": self._sequence + 1,
            "recorded_at": utc_now(),
            "mono_ns": monotonic_ns(),
            "clock_id": self.journal_id,
            "event": event,
            "data": dict(data),
        }
        if self.invocation_id is not None:
            record["invocation_id"] = self.invocation_id
        try:
            line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
        except (TypeError, ValueError):
            self._drop(label="record-serialize-failed")
            return False
        if self._descriptor is None:
            self._drop(label="journal-unavailable")
            return False
        # Written through the raw descriptor, not a buffered writer: a
        # buffered layer would keep a dropped line's bytes and resurrect
        # them out of order if a transient failure later recovered.
        payload = memoryview(line.encode("utf-8"))
        try:
            while payload:
                payload = payload[os.write(self._descriptor, payload) :]
        except OSError as error:
            self._drop(label=f"journal-write-failed-{_errno_code(error=error)}")
            if not self._truncate_to_flushed():
                # The recovery itself failed: the journal can no longer be
                # trusted to hold whole records, so it is closed for good
                # and the gap is counted instead of burying a torn line.
                self._abandon_journal()
                self._drop(label="journal-truncate-failed")
            return False
        self._bytes += len(line)
        self._sequence += 1
        return True

    def _truncate_to_flushed(self) -> bool:
        """Remove a partially written line and reposition; False on failure."""
        if self._descriptor is None:
            return False
        try:
            os.ftruncate(self._descriptor, self._bytes)
            os.lseek(self._descriptor, self._bytes, os.SEEK_SET)
        except OSError:
            return False
        return True

    def _abandon_journal(self) -> None:
        """Close the journal descriptor; no further writes are attempted."""
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        with contextlib.suppress(OSError):
            os.close(descriptor)

    def emit_terminal_state(self, *, source: str) -> None:
        """Emit the terminal collection_state record for this journal's source."""
        status = "stopped" if self.dropped_records == 0 and not self.errors else "degraded"
        self.emit(
            event="collection_state",
            data={
                "source": source,
                "status": status,
                "complete": status == "stopped",
                "covered_from": self.started_at,
                "covered_to": utc_now(),
                "dropped_records": self.dropped_records,
                "errors": [{"message": message} for message in self.errors[-MAX_ERROR_NOTES:]],
            },
        )

    def close(self, *, status: str) -> None:
        """Persist the writer status file and close the journal."""
        if self._closed:
            return
        self._closed = True
        if self._run_dirs.state is not None:
            payload = {
                "journal_id": self.journal_id,
                "status": status,
                "dropped_records": self.dropped_records,
                "errors": list(self.errors[-MAX_ERROR_NOTES:]),
            }
            _write_private_json(path=self._run_dirs.state / f"writer-status-{self.journal_id}.json", payload=payload)
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def finish(self, *, source: str) -> None:
        """Emit the terminal collection_state, then close as a plugin source."""
        status = "stopped" if self.dropped_records == 0 and not self.errors else "degraded"
        self.emit_terminal_state(source=source)
        self.close(status=status)


def _write_private_json(*, path: Path, payload: Mapping[str, object]) -> bool:
    """Exclusive create: foreign inodes, FIFOs and symlinks are never opened;

    only this call's own partial file is removed on failure.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        return False
    try:
        line = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        data = memoryview(line.encode("utf-8") + b"\n")
        while data:
            data = data[os.write(descriptor, data) :]
    except (OSError, TypeError, ValueError):
        with contextlib.suppress(OSError):
            os.unlink(path)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)
    return True


def _read_private_json(*, path: Path) -> object | None:
    """O_NOFOLLOW plus regular fstat and a bounded read; None for anything

    unreadable, non-regular, oversize or malformed.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_METADATA_BYTES:
                return None
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


@dataclass
class JobJournal:
    """Bootstrap-owned job journal: the plugin never writes these records, so

    a workflow with its own header never gets a duplicate.
    """

    writer: JournalWriter
    execution: Mapping[str, object]
    suite: Mapping[str, object]
    environment: Mapping[str, object]
    run_root: Path
    run_dirs: RunDirs
    started_mono: int = 0
    _native: list[Path] = field(default_factory=list)

    def finish(
        self, *, outcome: str, exit_code: int | None, end_reason: str, native_reports: Sequence[tuple[Path, str]] = ()
    ) -> Path:
        """Raises RuntimeError whenever the run cannot yield an honest bundle;

        the measured processes' results are never affected.
        """
        if self.writer.record_count == 0 or self.writer.dropped_records:
            raise RuntimeError("ci-observability job header was not persisted")
        first_failure = _earliest_invocation_failure(journals_dir=self.run_dirs.journals)
        data: dict[str, object] = {
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_ns": monotonic_ns() - self.started_mono,
        }
        if first_failure is not None:
            data["failure"] = first_failure
        self.writer.emit(event="execution_finished", data=data)
        if self.writer.dropped_records:
            raise RuntimeError("ci-observability job footer was not persisted")
        self.writer.close(status="stopped")
        self._copy_native_reports(native_reports=native_reports)
        manifest = self._build_manifest(end_reason=end_reason)
        bundle = self.run_root / "bundle"
        if not _write_private_json(path=bundle / MANIFEST_NAME, payload=manifest):
            raise RuntimeError("ci-observability manifest was not persisted")
        return bundle

    def _copy_native_reports(self, *, native_reports: Sequence[tuple[Path, str]]) -> None:
        if not native_reports:
            return
        if self.run_dirs.journals is None:
            raise RuntimeError("native reports cannot be added without a bundle")
        target_dir = self.run_root / "bundle" / "native-report"
        _ensure_private_dir(path=target_dir)
        for source, name in native_reports:
            if "/" in name or name in {".", ".."} or name == MANIFEST_NAME:
                raise RuntimeError("native report name is not canonical")
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("native report source is not a regular file")
            target = target_dir / name
            self._copy_exclusive(source=source, target=target)
            self._native.append(target)

    @staticmethod
    def _copy_exclusive(*, source: Path, target: Path) -> None:
        """Exclusive target, non-following non-blocking source re-checked regular

        after open (a post-precheck FIFO swap cannot stream in); failures
        remove the partial target and raise a fixed label.
        """
        try:
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise RuntimeError("native report source is not readable") from None
        target_fd: int | None = None
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise RuntimeError("native report source is not a regular file")
            try:
                target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            except FileExistsError:
                raise RuntimeError("native report target already exists") from None
            except OSError:
                raise RuntimeError("native report target is not writable") from None
            try:
                _copy_exclusive_fd(source_fd=source_fd, target_fd=target_fd)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(target)
                raise RuntimeError("native report copy failed") from None
            except RuntimeError:
                with contextlib.suppress(OSError):
                    os.unlink(target)
                raise
        finally:
            if target_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(target_fd)
            with contextlib.suppress(OSError):
                os.close(source_fd)

    def _build_manifest(self, *, end_reason: str) -> dict[str, object]:
        journals_dir = self.run_dirs.journals
        journal_paths = sorted(journals_dir.glob("*.jsonl")) if journals_dir is not None else []
        files: list[dict[str, object]] = []
        completeness_errors: list[dict[str, str]] = []
        dropped = 0
        all_complete = True

        status_by_journal = _writer_statuses(state_dir=self.run_dirs.state)
        execution_id = self.execution["execution_id"]
        for path in journal_paths:
            first_execution = _first_execution_id(path=path)
            if first_execution is not None and first_execution != execution_id:
                raise RuntimeError("journal from a different execution is present")
            digest, size, count, last_event = _scan_journal(path=path)
            complete = last_event in _TERMINAL_EVENTS
            all_complete = all_complete and complete
            files.append(
                {
                    "path": f"journals/{path.name}",
                    "kind": "journal",
                    "sha256": digest,
                    "size_bytes": size,
                    "record_count": count,
                    "complete": complete,
                }
            )
            status = status_by_journal.get(path.stem)
            if status is None:
                completeness_errors.append(
                    {"source": f"pytest-journal-{path.stem}", "message": "writer-status-missing"}
                )
                continue
            dropped += status["dropped_records"]
            completeness_errors.extend(
                {"source": f"pytest-journal-{path.stem}", "message": message} for message in status["errors"]
            )
        completeness_errors.extend({"source": "job-journal", "message": message} for message in self.writer.errors)
        for target in self._native:
            files.append(
                {
                    "path": f"native-report/{target.name}",
                    "kind": "native-report",
                    "sha256": _file_digest(path=target),
                    "size_bytes": target.stat().st_size,
                }
            )
        self._reject_unexpected_files()
        complete = all_complete and dropped == 0 and not completeness_errors
        return {
            "schema": MANIFEST_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
            "execution": dict(self.execution),
            "suite": dict(self.suite),
            "environment": dict(self.environment),
            "files": files,
            "completeness": {
                "complete": complete,
                "end_reason": end_reason,
                "dropped_records": dropped,
                "collection_errors": completeness_errors[:MAX_ERROR_NOTES],
            },
            "redaction": {"applied": False, "findings": 0},
        }

    def _reject_unexpected_files(self) -> None:
        bundle = self.run_root / "bundle"
        expected: set[Path] = set()
        if self.run_dirs.journals is not None:
            expected |= set(self.run_dirs.journals.glob("*.jsonl"))
        expected |= set(self._native)
        for path in bundle.rglob("*"):
            if path.is_file() and path not in expected:
                raise RuntimeError("unexpected file in bundle")


def _copy_exclusive_fd(*, source_fd: int, target_fd: int) -> None:
    total = 0
    while True:
        chunk = os.read(source_fd, _READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_NATIVE_REPORT_BYTES:
            raise RuntimeError("native report is oversize")
        view = memoryview(chunk)
        while view:
            view = view[os.write(target_fd, view) :]


def write_job_header(
    *, execution: Mapping[str, object], suite: Mapping[str, object], environment: Mapping[str, object], run_root: Path
) -> JobJournal:
    """Create the run directories and write the single execution_started record.

    ``execution``, ``suite`` and ``environment`` are the contract-shaped
    claims of the trusted workflow bootstrap (built by
    ``ExecutionClaims`` in ``context.py``). The manifest written by
    ``JobJournal.finish`` echoes them, satisfying the contract's identity
    echo check.
    """
    run_dirs = open_run(run_root=run_root)
    writer = JournalWriter(
        run_dirs=run_dirs, journal_id="controller", execution_id=str(execution["execution_id"]), invocation_id=None
    )
    if not writer.emit(event="execution_started", data={"job": execution["job"], "suite": suite}):
        raise RuntimeError("ci-observability job header was not persisted")
    job = JobJournal(
        writer=writer,
        execution=execution,
        suite=suite,
        environment=environment,
        run_root=run_root,
        run_dirs=run_dirs,
        started_mono=monotonic_ns(),
    )
    return job


def _writer_statuses(*, state_dir: Path | None) -> dict[str, dict[str, object]]:
    """A wrong-shaped status file is skipped so the footer reports the journal

    as missing: a visible gap, never a crash or a guessed sum.
    """
    if state_dir is None:
        return {}
    statuses: dict[str, dict[str, object]] = {}
    for path in sorted(state_dir.glob("writer-status-*.json")):
        payload = _read_private_json(path=path)
        if not isinstance(payload, dict):
            continue
        journal_id = payload.get("journal_id")
        dropped = payload.get("dropped_records")
        errors = payload.get("errors")
        if (
            not isinstance(journal_id, str)
            or not isinstance(dropped, int)
            or isinstance(dropped, bool)
            or not isinstance(errors, list)
            or not all(isinstance(message, str) for message in errors)
        ):
            continue
        statuses[journal_id] = payload
    return statuses


def _scan_journal(*, path: Path) -> tuple[str, int, int, str | None]:
    """Checksum, size, record count, and last event of one journal file."""
    digest = hashlib.sha256()
    size = 0
    count = 0
    last_event: str | None = None
    last_line = b""
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            size += len(line)
            if line.strip():
                count += 1
                last_line = line
    try:
        last_event = json.loads(last_line.decode("utf-8")).get("event")
    except (ValueError, UnicodeDecodeError):
        last_event = None
    return digest.hexdigest(), size, count, last_event


def _first_execution_id(*, path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            first = handle.readline()
        return str(json.loads(first.decode("utf-8"))["execution_id"])
    except (OSError, ValueError, UnicodeDecodeError, KeyError):
        return None


def _earliest_invocation_failure(*, journals_dir: Path | None) -> dict[str, object] | None:
    """The earliest first failure claimed by an invocation_finished record."""
    if journals_dir is None:
        return None
    best: dict[str, object] | None = None
    best_at = ""
    for path in sorted(journals_dir.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") != "invocation_finished":
                continue
            failure = record.get("data", {}).get("failure")
            recorded_at = record.get("recorded_at", "")
            if failure and (best is None or recorded_at < best_at):
                best, best_at = failure, recorded_at
    return best


def _file_digest(*, path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_collection_sidecar(*, state_dir: Path | None, journal_id: str, payload: Mapping[str, object]) -> bool:
    """Persist one worker's private collection metadata for the controller."""
    if state_dir is None:
        return False
    return _write_private_json(path=state_dir / f"collection-{journal_id}.json", payload=payload)


def read_collection_sidecar(*, state_dir: Path | None, journal_id: str) -> dict[str, object] | None:
    """Only the supported shape is accepted; anything else is None so the

    controller degrades collection instead of trusting or crashing.
    """
    if state_dir is None:
        return None
    payload = _read_private_json(path=state_dir / f"collection-{journal_id}.json")
    if not isinstance(payload, dict):
        return None
    collected = payload.get("collected")
    deselected = payload.get("deselected", [])
    if not isinstance(collected, list) or not all(isinstance(test_id, str) for test_id in collected):
        return None
    if not isinstance(deselected, list):
        return None
    for entry in deselected:
        if not isinstance(entry, dict) or not isinstance(entry.get("test_id"), str):
            return None
    return payload
