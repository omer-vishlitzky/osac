#!/usr/bin/env python3
"""Keep private native Go/Ginkgo reports and each reporting command's exit code.

Reports stay local and require redaction before upload. Bookkeeping failures
are collection errors, separate from the child's result.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import fnmatch
import json
import math
import os
import signal
import stat
import subprocess
import sys
import time
import types
from collections import Counter
from pathlib import Path
from typing import BinaryIO

FRAMEWORKS = ("go-test", "ginkgo")
GO_TEST_REPORT_NAME = "go-test.json"
GINKGO_REPORT_NAME = "ginkgo-report.json"
# Suite-side collectors published by the components' test-support hooks read
# this variable and write one "ginkgo-report-<random>.json" final per suite
# run; the random suffix comes from the collector's exclusive temporary name.
GINKGO_REPORT_ENV = "OSAC_CI_GINKGO_REPORT_DIR"
GINKGO_FINAL_REPORT_GLOB = "ginkgo-report-*.json"
GINKGO_PARTIAL_REPORT_GLOB = ".ginkgo-report-*.json.partial"
GINKGO_REPORT_EVIDENCE_ERROR = "ginkgo left no report evidence; test outcome is unknown"
GINKGO_MERGE_ERROR = "ginkgo report merge failed"
GINKGO_FINAL_NOT_REGULAR_ERROR = "ginkgo report final is not a regular file"
GINKGO_FINAL_SIZE_ERROR = "ginkgo report final exceeds the collection size limit"
GINKGO_FINAL_JSON_ERROR = "ginkgo report final is not valid JSON"
GINKGO_PARTIAL_LEFT_ERROR = "ginkgo report collection left unpublished partial evidence"
GINKGO_EXPECTED_MISSING_PREFIX = "ginkgo expected suite report missing:"
# Collection caps. A native report is contracted to stay below 32 MiB; the
# totals keep a pathological attempt from exhausting memory or disk.
GINKGO_FINAL_MAX_BYTES = 32 * 1024 * 1024
GINKGO_TOTAL_MAX_BYTES = 256 * 1024 * 1024
GINKGO_FINAL_MAX_COUNT = 1000
ATTEMPT_MAX_ENTRIES = 4096
GINKGO_EXPECTED_MAX_BYTES = 128 * 1024
GINKGO_JSON_MAX_DEPTH = 64
ATTEMPT_ENTRY_LIMIT_ERROR = "attempt directory exceeds the collection entry limit"
STATUS_NAME = "status.json"
REDACTION_MARKER_NAME = "REDACTION_REQUIRED"

ESCALATION_GRACE_SECONDS = 10

UNSUPPORTED_GINKGO_SUBCOMMANDS = ("build", "bootstrap", "convert", "generate", "labels", "unfocus", "version", "watch")

# Boolean spellings go test's flag parser accepts, matching strconv.ParseBool.
GO_FLAG_TRUE = frozenset({"1", "t", "T", "true", "TRUE", "True"})
GO_FLAG_FALSE = frozenset({"0", "f", "F", "false", "FALSE", "False"})
# Values such as `-run -json` belong to the preceding flag, not to this helper.
GO_FLAGS_WITH_VALUES = frozenset(
    {
        "asmflags",
        "bench",
        "benchtime",
        "blockprofile",
        "blockprofilerate",
        "buildmode",
        "compiler",
        "count",
        "covermode",
        "coverpkg",
        "coverprofile",
        "cpu",
        "cpuprofile",
        "exec",
        "fuzz",
        "fuzzminimizetime",
        "fuzztime",
        "gccgoflags",
        "gcflags",
        "installsuffix",
        "ldflags",
        "list",
        "memprofile",
        "memprofilerate",
        "mod",
        "modfile",
        "mutexprofile",
        "mutexprofilefraction",
        "o",
        "outputdir",
        "overlay",
        "p",
        "parallel",
        "pgo",
        "pkgdir",
        "run",
        "shuffle",
        "skip",
        "tags",
        "timeout",
        "toolexec",
        "trace",
        "vet",
    }
)

USAGE_ERROR_EXIT = 2
MISSING_COMMAND_EXIT = 127
COLLECTION_ERROR_PREFIX = "native_reports: collection error:"

REDACTION_MARKER_TEXT = """Private, unredacted test evidence.

The files in this directory are raw output produced by a test framework
under OSAC CI observability native-report collection. They may contain
secrets, tokens, tenant names, host names, or paths.

Rules:
- Do not upload, publish, or attach these files anywhere without the
  OSAC-approved redaction process.
- The helper that wrote this directory never uploads it.
- This marker and status.json are local helper bookkeeping, not CI
  observability contract events.
"""

_STATUS_FORMAT = "osac-native-reports-local-status-1"
_STATUS_NOTE = "local helper bookkeeping; not a CI observability contract event"


class NativeReportError(Exception):
    """A configuration or reporting problem that stopped the helper."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class NativeReportResult:
    """Child result and independent collection errors."""

    framework: str
    attempt_dir: Path
    report_path: Path
    child_returncode: int | None
    exit_code: int
    interrupted: bool
    forced_kill: bool
    collection_errors: list[str]


def validate_command(*, framework: str, command: list[str]) -> None:
    """Fail before starting anything when the command is not a supported shape."""
    if framework not in FRAMEWORKS:
        raise NativeReportError(f"unsupported framework {framework!r}; expected one of: {', '.join(FRAMEWORKS)}")
    if not command:
        raise NativeReportError("command must not be empty; pass the test command after --")
    if framework == "go-test":
        _validate_go_test_command(command=command)
        return
    if os.path.basename(command[0]) != "ginkgo":
        raise NativeReportError("ginkgo framework expects a command that starts with: ginkgo ...")
    if len(command) > 1 and command[1] in UNSUPPORTED_GINKGO_SUBCOMMANDS:
        raise NativeReportError(f"ginkgo {command[1]} does not run specs; only the ginkgo run form is supported")
    index = 1
    while index < len(command):
        argument = command[index]
        if argument == "--":
            break
        if argument.startswith("-") and argument.lstrip("-") in (
            "focus",
            "skip",
            "label-filter",
            "focus-file",
            "skip-file",
            "sem-ver-filter",
        ):
            index += 2
            continue
        if _is_flag_argument(argument=argument, name="json-report") or _is_flag_argument(
            argument=argument, name="output-dir"
        ):
            raise NativeReportError(
                "command already sets --json-report or --output-dir; "
                "remove them so the helper's non-failing collector is the only report writer"
            )
        index += 1


def build_go_test_argv(*, command: list[str]) -> list[str]:
    if _caller_json_setting(command=command) in GO_FLAG_TRUE:
        return list(command)
    return [command[0], command[1], "-json", *command[2:]]


def create_attempt_dir(*, output_dir: Path) -> Path:
    root = output_dir.resolve()
    root_existed = root.exists()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        raise NativeReportError(f"output dir exists and is not a directory: {output_dir}") from None
    if root.parent == root:
        raise NativeReportError("output dir must not be the filesystem root")
    if not root_existed:
        os.chmod(root, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for counter in range(1, 1000):
        candidate = root / f"attempt-{stamp}-{os.getpid()}-{counter}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(candidate, 0o700)
        return candidate
    raise NativeReportError(f"could not create a unique attempt directory under {root}")


def write_redaction_marker(*, attempt_dir: Path) -> None:
    fd = _create_private_file(path=attempt_dir / REDACTION_MARKER_NAME)
    with os.fdopen(fd, "w", encoding="utf-8") as marker_file:
        marker_file.write(REDACTION_MARKER_TEXT)


def run_with_native_report(
    *, framework: str, output_dir: Path, command: list[str], ginkgo_expected_suites: list[str] | None = None
) -> NativeReportResult:
    """Run one test command and keep its native report in a private attempt dir."""
    validate_command(framework=framework, command=command)
    if ginkgo_expected_suites is not None and framework != "ginkgo":
        raise NativeReportError("--ginkgo-expected-suites requires --framework ginkgo")
    if ginkgo_expected_suites is not None:
        _validate_expected_suites(suites=ginkgo_expected_suites)
    attempt_dir = create_attempt_dir(output_dir=output_dir)
    write_redaction_marker(attempt_dir=attempt_dir)
    report_name = GO_TEST_REPORT_NAME if framework == "go-test" else GINKGO_REPORT_NAME
    record = _RunRecord(framework=framework, attempt=attempt_dir.name, start_utc=_utc_now(), report_name=report_name)
    _write_status(attempt_dir=attempt_dir, record=record)
    try:
        if framework == "go-test":
            exit_code = _run_go_test(attempt_dir=attempt_dir, command=command, record=record)
        else:
            exit_code = _run_ginkgo(
                attempt_dir=attempt_dir, command=command, record=record, expected_suites=ginkgo_expected_suites
            )
    except FileNotFoundError:
        record.errors.append(f"test command executable not found: {os.path.basename(command[0])}")
        exit_code = MISSING_COMMAND_EXIT
    record.state = "finished"
    record.end_utc = _utc_now()
    record.exit_code = exit_code
    errors_before_inventory = len(record.errors)
    _finalize_attempt(attempt_dir=attempt_dir, record=record)
    if framework == "ginkgo" and len(record.errors) != errors_before_inventory:
        record.coverage = {"state": "unknown"}
    _persist_status(attempt_dir=attempt_dir, record=record)
    if not _write_collection_errors(errors=record.errors):
        record.errors.append("collection diagnostic write failed")
        if framework == "ginkgo":
            record.coverage = {"state": "unknown"}
        _persist_status(attempt_dir=attempt_dir, record=record)
    return NativeReportResult(
        framework=framework,
        attempt_dir=attempt_dir,
        report_path=attempt_dir / report_name,
        child_returncode=record.child_returncode,
        exit_code=exit_code,
        interrupted=record.interrupted,
        forced_kill=record.forced_kill,
        collection_errors=list(record.errors),
    )


@dataclasses.dataclass(kw_only=True)
class _RunRecord:
    """Mutable local bookkeeping for one attempt; serialized to status.json."""

    framework: str
    attempt: str
    start_utc: str
    report_name: str
    state: str = "running"
    end_utc: str | None = None
    child_pid: int | None = None
    child_returncode: int | None = None
    exit_code: int | None = None
    interrupted: bool = False
    signal: int | None = None
    forced_kill: bool = False
    json_event_lines: int | None = None
    non_json_lines: int | None = None
    artifacts: list[dict[str, object]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    coverage: dict[str, object] | None = None


def _validate_go_test_command(*, command: list[str]) -> None:
    if os.path.basename(command[0]) != "go" or len(command) < 2 or command[1] != "test":
        raise NativeReportError("go-test framework expects a command that starts with: go test ...")
    json_setting = _caller_json_setting(command=command)
    if json_setting is None or json_setting in GO_FLAG_TRUE:
        return
    if json_setting in GO_FLAG_FALSE:
        raise NativeReportError("command disables go test -json; the helper cannot record a structured report")
    raise NativeReportError("command passes go test -json a value that is not a boolean")


def _caller_json_setting(*, command: list[str]) -> str | None:
    setting: str | None = None
    index = 2
    while index < len(command):
        token = command[index]
        if token in ("-args", "--args", "--"):
            break
        name = token.lstrip("-").removeprefix("test.")
        if token.startswith("-") and name in GO_FLAGS_WITH_VALUES:
            index += 2
            continue
        if token in ("-json", "--json"):
            setting = "true"
        elif token.startswith("-json=") or token.startswith("--json="):
            setting = token.partition("=")[2]
        index += 1
    return setting


def _run_go_test(*, attempt_dir: Path, command: list[str], record: _RunRecord) -> int:
    argv = build_go_test_argv(command=command)
    report_path = attempt_dir / GO_TEST_REPORT_NAME
    report_fd = _create_private_file(path=report_path)
    try:
        child = _popen_child(argv=argv, pipe_stdout=True)
    except FileNotFoundError:
        os.close(report_fd)
        raise
    record.child_pid = child.pid
    with _ChildSignalWatch(child=child) as watch:
        _persist_status(attempt_dir=attempt_dir, record=record)
        assert child.stdout is not None
        report_file = os.fdopen(report_fd, "wb")
        tee = _StreamTee(report_file=report_file, console=sys.stdout.buffer)
        try:
            for raw_line in child.stdout:
                tee.forward(raw_line=raw_line)
            returncode = child.wait()
        finally:
            try:
                report_file.close()
            except OSError:
                tee.errors.append("report stream close failed")
    record.child_returncode = returncode
    record.interrupted = watch.signum is not None
    record.signal = watch.signum
    record.forced_kill = watch.forced_kill
    record.json_event_lines = tee.json_event_lines
    record.non_json_lines = tee.non_json_lines
    record.errors.extend(tee.errors)
    return _final_exit_code(returncode=returncode, signum=watch.signum)


def _run_ginkgo(*, attempt_dir: Path, command: list[str], record: _RunRecord, expected_suites: list[str] | None) -> int:
    # The collector lives in the suites, not in the CLI: reporting flags stay
    # untouched and the private attempt directory is named by an environment
    # variable, so a report IO failure can never change the CLI's exit code.
    child_env = dict(os.environ)
    child_env[GINKGO_REPORT_ENV] = str(attempt_dir)
    child = _popen_child(argv=list(command), pipe_stdout=False, env=child_env)
    record.child_pid = child.pid
    with _ChildSignalWatch(child=child) as watch:
        _persist_status(attempt_dir=attempt_dir, record=record)
        returncode = child.wait()
    record.child_returncode = returncode
    record.interrupted = watch.signum is not None
    record.signal = watch.signum
    record.forced_kill = watch.forced_kill
    _collect_ginkgo_reports(attempt_dir=attempt_dir, record=record, expected_suites=expected_suites)
    return _final_exit_code(returncode=returncode, signum=watch.signum)


class _GinkgoFinalError(Exception):
    """A rejected collector final, carrying a constant collection-error label."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


def _collect_ginkgo_reports(*, attempt_dir: Path, record: _RunRecord, expected_suites: list[str] | None) -> None:
    """File presence cannot establish coverage without a caller catalog."""
    record.coverage = {"state": "unknown"}
    try:
        entries = _attempt_entries(attempt_dir=attempt_dir)
    except _GinkgoFinalError as error:
        record.errors.append(error.label)
        return
    finals: list[os.DirEntry[str]] = []
    try:
        for entry in entries:
            if fnmatch.fnmatch(entry.name, GINKGO_PARTIAL_REPORT_GLOB):
                record.errors.append(GINKGO_PARTIAL_LEFT_ERROR)
                continue
            if not fnmatch.fnmatch(entry.name, GINKGO_FINAL_REPORT_GLOB):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                record.errors.append(GINKGO_FINAL_NOT_REGULAR_ERROR)
                continue
            finals.append(entry)
            if len(finals) > GINKGO_FINAL_MAX_COUNT:
                record.errors.append(GINKGO_FINAL_SIZE_ERROR)
                return
    except OSError:
        record.errors.append(GINKGO_MERGE_ERROR)
        return
    if record.interrupted or record.errors:
        return
    merged: list[bytes] = []
    descriptions: Counter[str] = Counter()
    total_bytes = 0
    for entry in finals:
        remaining_bytes = GINKGO_TOTAL_MAX_BYTES - total_bytes
        if remaining_bytes <= 0:
            record.errors.append(GINKGO_FINAL_SIZE_ERROR)
            return
        try:
            description, payload, size = _read_ginkgo_final(
                path=Path(entry.path), byte_limit=min(GINKGO_FINAL_MAX_BYTES, remaining_bytes)
            )
        except _GinkgoFinalError as failure:
            record.errors.append(failure.label)
            return
        total_bytes += size
        if total_bytes > GINKGO_TOTAL_MAX_BYTES:
            record.errors.append(GINKGO_FINAL_SIZE_ERROR)
            return
        merged.append(payload)
        descriptions[description] += 1
    if merged and not _write_ginkgo_merged(attempt_dir=attempt_dir, merged=merged):
        record.errors.append(GINKGO_MERGE_ERROR)
        return
    if expected_suites is None:
        return
    expected = Counter(expected_suites)
    missing = sorted((expected - descriptions).elements())
    record.coverage = {
        "state": "incomplete" if missing else "complete",
        "expected": len(expected_suites),
        "published": sum(descriptions.values()),
    }
    if missing:
        record.coverage["missing"] = missing
        # A missing expected suite is a source gap; the exit code stays the
        # child's own and no test outcome is invented.
        record.errors.append(f"{GINKGO_EXPECTED_MISSING_PREFIX} {len(missing)}")


def _attempt_entries(*, attempt_dir: Path) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(attempt_dir) as directory:
            for entry in directory:
                if len(entries) >= ATTEMPT_MAX_ENTRIES:
                    raise _GinkgoFinalError(ATTEMPT_ENTRY_LIMIT_ERROR)
                entries.append(entry)
    except OSError:
        raise _GinkgoFinalError(GINKGO_MERGE_ERROR) from None
    return sorted(entries, key=lambda entry: entry.name)


def _read_capped_regular_file(*, path: Path, byte_limit: int) -> bytes:
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _GinkgoFinalError(GINKGO_FINAL_NOT_REGULAR_ERROR)
                if info.st_size > byte_limit:
                    raise _GinkgoFinalError(GINKGO_FINAL_SIZE_ERROR)
                chunks: list[bytes] = []
                remaining = byte_limit + 1
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 1 << 20))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise _GinkgoFinalError(GINKGO_MERGE_ERROR) from None
    data = b"".join(chunks)
    if len(data) > byte_limit:
        raise _GinkgoFinalError(GINKGO_FINAL_SIZE_ERROR)
    return data


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite-json-number")


def _report_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite-json-number")
    return number


def _report_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate-json-key")
        values[key] = value
    return values


def _check_report_depth(*, value: object, depth: int = 0) -> None:
    if depth > GINKGO_JSON_MAX_DEPTH:
        raise ValueError("json-depth-limit")
    if isinstance(value, dict):
        for item in value.values():
            _check_report_depth(value=item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_report_depth(value=item, depth=depth + 1)


def _read_ginkgo_final(*, path: Path, byte_limit: int = GINKGO_FINAL_MAX_BYTES) -> tuple[str, bytes, int]:
    data = _read_capped_regular_file(path=path, byte_limit=byte_limit)
    try:
        document = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_report_float,
            object_pairs_hook=_report_pairs,
        )
        _check_report_depth(value=document)
    except (ValueError, RecursionError):
        raise _GinkgoFinalError(GINKGO_FINAL_JSON_ERROR) from None
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise _GinkgoFinalError(GINKGO_FINAL_JSON_ERROR)
    description = document[0].get("SuiteDescription")
    if not isinstance(description, str) or not description:
        raise _GinkgoFinalError(GINKGO_FINAL_JSON_ERROR)
    # Merge these exact validated bytes, not a second read of mutable files.
    return description, data.strip()[1:-1], len(data)


def _write_ginkgo_merged(*, attempt_dir: Path, merged: list[bytes]) -> bool:
    tmp_path = attempt_dir / f"{GINKGO_REPORT_NAME}.merge.tmp"
    try:
        with os.fdopen(_create_private_file(path=tmp_path), "wb") as merged_file:
            merged_file.write(b"[")
            for index, report in enumerate(merged):
                if index:
                    merged_file.write(b",\n")
                merged_file.write(report)
            merged_file.write(b"]\n")
        os.replace(tmp_path, attempt_dir / GINKGO_REPORT_NAME)
    except (OSError, ValueError, RecursionError):
        # The finals stay in place as evidence when the merge cannot run.
        return False
    return True


def load_ginkgo_expected_suites(*, path: Path) -> list[str]:
    """Read the trusted manifest of suite descriptions expected to publish."""
    try:
        text = _read_capped_regular_file(path=path, byte_limit=GINKGO_EXPECTED_MAX_BYTES).decode("utf-8")
    except (_GinkgoFinalError, UnicodeError):
        raise NativeReportError("cannot read ginkgo expected suites manifest") from None
    suites: list[str] = []
    for line in text.splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            suites.append(entry)
    _validate_expected_suites(suites=suites)
    return sorted(suites)


def _validate_expected_suites(*, suites: list[str]) -> None:
    if not suites:
        raise NativeReportError("ginkgo expected suites manifest is empty")
    if len(suites) > GINKGO_FINAL_MAX_COUNT:
        raise NativeReportError("ginkgo expected suites manifest exceeds the entry limit")
    for suite in suites:
        if (
            not isinstance(suite, str)
            or not suite.strip()
            or len(suite) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in suite)
        ):
            raise NativeReportError("invalid ginkgo expected suite identity")


def _popen_child(*, argv: list[str], pipe_stdout: bool, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    stdout = subprocess.PIPE if pipe_stdout else None
    return subprocess.Popen(argv, stdout=stdout, env=env, start_new_session=True, umask=0o077)


def _persist_status(*, attempt_dir: Path, record: _RunRecord) -> None:
    try:
        _write_status(attempt_dir=attempt_dir, record=record)
    except OSError:
        record.errors.append("status write failed")
        if record.framework == "ginkgo":
            record.coverage = {"state": "unknown"}


def _write_collection_errors(*, errors: list[str]) -> bool:
    try:
        for error in errors:
            print(f"{COLLECTION_ERROR_PREFIX} {error}", file=sys.stderr)
    except OSError:
        return False
    return True


class _StreamTee:
    """Drain the child even after a report or console write fails."""

    def __init__(self, *, report_file: BinaryIO, console: BinaryIO) -> None:
        self._report_file = report_file
        self._console = console
        self.errors: list[str] = []
        self.json_event_lines = 0
        self.non_json_lines = 0
        self._report_broken = False
        self._console_broken = False

    def forward(self, *, raw_line: bytes) -> None:
        event = _parse_json_object(raw_line=raw_line)
        self._write_report(raw_line=raw_line, is_event=event is not None)
        self._write_console(raw_line=raw_line, event=event)

    def _write_report(self, *, raw_line: bytes, is_event: bool) -> None:
        if self._report_broken:
            return
        try:
            self._report_file.write(raw_line)
            self._report_file.flush()
        except OSError:
            self._report_broken = True
            self.errors.append("report stream write failed")
            return
        if is_event:
            self.json_event_lines += 1
        else:
            self.non_json_lines += 1

    def _write_console(self, *, raw_line: bytes, event: dict[str, object] | None) -> None:
        if self._console_broken:
            return
        try:
            self._console.write(_console_text(raw_line=raw_line, event=event))
            self._console.flush()
        except OSError:
            self._console_broken = True
            self.errors.append("console stream write failed")


def _console_text(*, raw_line: bytes, event: dict[str, object] | None) -> bytes:
    if event is None:
        return raw_line
    output = event.get("Output")
    if isinstance(output, str):
        return output.encode("utf-8", errors="replace")
    return b""


def _parse_json_object(*, raw_line: bytes) -> dict[str, object] | None:
    try:
        document = json.loads(raw_line)
    except (ValueError, RecursionError):
        return None
    if isinstance(document, dict):
        return document
    return None


def _final_exit_code(*, returncode: int, signum: int | None) -> int:
    if signum is not None:
        return 128 + signum
    if returncode < 0:
        return 128 - returncode
    return returncode


def _create_private_file(*, path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return fd


def _write_status(*, attempt_dir: Path, record: _RunRecord) -> None:
    document: dict[str, object] = dataclasses.asdict(record)
    document["format"] = _STATUS_FORMAT
    document["note"] = _STATUS_NOTE
    tmp_path = attempt_dir / f"{STATUS_NAME}.tmp"
    with os.fdopen(_create_private_file(path=tmp_path), "w", encoding="utf-8") as status_file:
        json.dump(document, status_file, indent=2, sort_keys=True)
        status_file.write("\n")
    os.replace(tmp_path, attempt_dir / STATUS_NAME)


def _finalize_attempt(*, attempt_dir: Path, record: _RunRecord) -> None:
    try:
        entries = _attempt_entries(attempt_dir=attempt_dir)
    except _GinkgoFinalError as error:
        record.errors.append(error.label)
        return
    has_ginkgo_report = False
    for directory_entry in entries:
        entry = Path(directory_entry.path)
        if entry.name in (STATUS_NAME, f"{STATUS_NAME}.tmp", REDACTION_MARKER_NAME):
            continue
        try:
            metadata = directory_entry.stat(follow_symlinks=False)
        except OSError:
            record.errors.append("artifact size unavailable")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            record.errors.append("unexpected symlink in attempt directory")
            continue
        is_file = stat.S_ISREG(metadata.st_mode)
        is_directory = stat.S_ISDIR(metadata.st_mode)
        size = metadata.st_size
        if fnmatch.fnmatch(entry.name, GINKGO_FINAL_REPORT_GLOB) and is_file and size > 0:
            has_ginkgo_report = True
        if (is_file and metadata.st_nlink == 1) or is_directory:
            try:
                descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    current = os.fstat(descriptor)
                    if (stat.S_ISREG(current.st_mode) and current.st_nlink == 1) or stat.S_ISDIR(current.st_mode):
                        os.fchmod(descriptor, 0o700 if stat.S_ISDIR(current.st_mode) else 0o600)
                    else:
                        record.errors.append("artifact type changed during collection")
                finally:
                    os.close(descriptor)
            except OSError:
                record.errors.append("permission sweep failed")
        elif is_file:
            record.errors.append("unexpected hardlink in attempt directory")
        artifact: dict[str, object] = {"name": entry.name, "bytes": size}
        if entry.name == record.report_name and record.json_event_lines is not None:
            artifact["json_event_lines"] = record.json_event_lines
            artifact["non_json_lines"] = record.non_json_lines
        record.artifacts.append(artifact)
    if (
        record.framework == "ginkgo"
        and record.child_returncode is not None
        and not record.interrupted
        and not has_ginkgo_report
    ):
        # Ginkgo can fail its own reporting hook after every spec passes.
        record.errors.append(GINKGO_REPORT_EVIDENCE_ERROR)


def _is_flag_argument(*, argument: str, name: str) -> bool:
    return argument in (f"-{name}", f"--{name}") or argument.startswith((f"-{name}=", f"--{name}="))


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _ChildSignalWatch:
    """Forward the first stop reason and kill remaining children after interruption."""

    def __init__(self, *, child: subprocess.Popen[bytes]) -> None:
        self.child = child
        self.signum: int | None = None
        self.forced_kill = False
        self._previous: dict[int, signal.Handlers] = {}

    def _signal_child(self, *, signum: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.child.pid, signum)

    def _forward(self, signum: int, frame: types.FrameType | None) -> None:
        if self.signum is None:
            if self.child.poll() is not None:
                return
            self.signum = signum
            signal.alarm(ESCALATION_GRACE_SECONDS)
        self._signal_child(signum=signum)

    def _escalate(self, signum: int, frame: types.FrameType | None) -> None:
        self.forced_kill = True
        self._signal_child(signum=signal.SIGKILL)

    def __enter__(self) -> _ChildSignalWatch:
        self._previous = {
            signal.SIGINT: signal.signal(signal.SIGINT, self._forward),
            signal.SIGTERM: signal.signal(signal.SIGTERM, self._forward),
            signal.SIGALRM: signal.signal(signal.SIGALRM, self._escalate),
        }
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        signal.alarm(0)
        if self.signum is not None:
            # A group leader may exit while descendants still ignore the stop signal.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child.pid, signal.SIGKILL)
                self.forced_kill = True
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="native_reports.py",
        description=(
            "Run one trusted CI test command with its native report kept in a private "
            "output directory. The reporting command's exit code is returned unchanged. "
            "Ginkgo report collection is non-failing; missing evidence is a collection error."
        ),
    )
    parser.add_argument("--framework", required=True, choices=FRAMEWORKS, help="test framework of the command")
    parser.add_argument("--output-dir", required=True, help="private directory that receives the report files")
    parser.add_argument(
        "--ginkgo-expected-suites",
        metavar="PATH",
        default=None,
        help=(
            "trusted manifest with one expected suite description per line; "
            "ginkgo collection coverage is unknown without it"
        ),
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="test command to run, passed after -- so its flags are not parsed here",
    )
    return parser


def main(*, argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    expected_suites: list[str] | None = None
    if args.ginkgo_expected_suites is not None:
        if args.framework != "ginkgo":
            print("native_reports: --ginkgo-expected-suites requires --framework ginkgo", file=sys.stderr)
            return USAGE_ERROR_EXIT
        try:
            expected_suites = load_ginkgo_expected_suites(path=Path(args.ginkgo_expected_suites))
        except NativeReportError as error:
            print(f"native_reports: {error}", file=sys.stderr)
            return USAGE_ERROR_EXIT
    try:
        result = run_with_native_report(
            framework=args.framework,
            output_dir=Path(args.output_dir),
            command=command,
            ginkgo_expected_suites=expected_suites,
        )
    except NativeReportError as error:
        print(f"native_reports: {error}", file=sys.stderr)
        return USAGE_ERROR_EXIT
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    _defuse_broken_streams()
    return result.exit_code


def _defuse_broken_streams() -> None:
    # Python's shutdown flush must not replace the child's exit code with 120.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), stream.fileno())


if __name__ == "__main__":
    sys.exit(main())
