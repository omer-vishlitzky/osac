"""Native CI report generation for go test and ginkgo, through the real CLI.

The Python-only tests use fake runner executables to pin the helper's CLI
contract: flag injection, exit-code preservation, private output layout,
collision handling, and collection-error surfacing. The real-runner tests
execute the tiny Go and Ginkgo fixture suites under
tests/unit/fixtures/ci_observability_native/ and skip visibly when the
framework executable is unavailable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tools.ci_observability.native_reports import (
    GINKGO_REPORT_NAME,
    GO_TEST_REPORT_NAME,
    MISSING_COMMAND_EXIT,
    REDACTION_MARKER_NAME,
    STATUS_NAME,
    USAGE_ERROR_EXIT,
    NativeReportError,
    build_ginkgo_argv,
    build_go_test_argv,
    build_parser,
    create_attempt_dir,
    run_with_native_report,
    validate_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "tools" / "ci_observability" / "native_reports.py"
FIXTURES = REPO_ROOT / "tests" / "unit" / "fixtures" / "ci_observability_native"

GO_BIN = shutil.which("go")
_GINKGO_DEFAULT_PATH = Path.home() / "go" / "bin" / "ginkgo"
GINKGO_BIN = shutil.which("ginkgo") or (str(_GINKGO_DEFAULT_PATH) if _GINKGO_DEFAULT_PATH.exists() else None)

requires_go = pytest.mark.skipif(GO_BIN is None, reason="go toolchain not available")
requires_ginkgo = pytest.mark.skipif(GINKGO_BIN is None, reason="ginkgo CLI not available")

# A fake go test -json stream: two events without console text, one event
# with an Output field, and one plain non-JSON line. The expected report bytes
# stay next to the script so both sides of the contract are visible here.
FAKE_GO_STREAM_SCRIPT = """printf '%s\\n' '{"Action":"start","Package":"example.com/fake"}'
printf '%s\\n' '{"Action":"output","Package":"example.com/fake","Test":"TestFake","Output":"fake console line\\n"}'
printf '%s\\n' 'plain text the framework might emit'
printf '%s\\n' '{"Action":"pass","Package":"example.com/fake","Test":"TestFake","Elapsed":0.01}'
exit 7"""

FAKE_GO_STREAM_REPORT = (
    b'{"Action":"start","Package":"example.com/fake"}\n'
    b'{"Action":"output","Package":"example.com/fake","Test":"TestFake","Output":"fake console line\\n"}\n'
    b"plain text the framework might emit\n"
    b'{"Action":"pass","Package":"example.com/fake","Test":"TestFake","Elapsed":0.01}\n'
)

# A fake ginkgo CLI: it must receive the helper's report flags and write the
# report itself, proving the plumbing the real ginkgo relies on.
FAKE_GINKGO_SCRIPT = """output_dir=""
report_name=""
for argument in "$@"; do
    case "$argument" in
        --output-dir=*) output_dir="${argument#--output-dir=}" ;;
        --json-report=*) report_name="${argument#--json-report=}" ;;
    esac
done
printf '%s' '[{"SuiteDescription": "fake ginkgo suite", "SuiteSucceeded": true}]' > "${output_dir}/${report_name}"
echo "fake ginkgo console"
exit 5"""

FAKE_GINKGO_REPORT = b'[{"SuiteDescription": "fake ginkgo suite", "SuiteSucceeded": true}]'

# A fake go that waits for the persisted child pid, then locks the attempt
# directory against the helper's final status write.
FAKE_GO_LOCK_DIR_SCRIPT = """printf '%s\\n' '{"Action":"output","Output":"test passed\\n"}'
waited=0
until grep -q '"child_pid": [0-9]' "$FAKE_GO_DIR"/*/status.json 2>/dev/null || [ "$waited" -ge 100 ]; do
    sleep 0.05
    waited=$((waited + 1))
done
chmod 500 "$FAKE_GO_DIR"/*/
exit 0"""

FAKE_GO_LOCK_DIR_REPORT = b'{"Action":"output","Output":"test passed\\n"}\n'

# A fake go that traps both stop signals, so only the helper's escalation
# can end it.
FAKE_GO_SIGNAL_TRAP_SCRIPT = """trap "" INT TERM
printf '%s\\n' '{"Action":"start"}'
sleep 30
exit 0"""


@pytest.fixture
def stable_dir() -> Iterator[Path]:
    """A temp directory outside pytest's managed tree, for subprocess cwd.

    pytest's per-test tmp_path directories carry no cleanup lock, and under
    the full unit suite its numbered-directory cleanup has deleted a running
    test's tree, taking a framework subprocess's working directory and the
    helper's output with it. This directory is owned by the test alone and
    lives until the test ends.
    """
    directory = Path(tempfile.mkdtemp(prefix="ci-native-reports-", dir=REPO_ROOT.parent))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def run_helper(
    framework: str,
    output_dir: Path,
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[bytes]:
    """Run the helper CLI exactly the way CI wiring will invoke it."""
    argv = [sys.executable, str(HELPER), "--framework", framework, "--output-dir", str(output_dir), "--", *command]
    return subprocess.run(argv, capture_output=True, cwd=cwd, env=env, timeout=timeout, check=False)


def _write_fake_runner(directory: Path, name: str, body: str) -> None:
    program = directory / name
    program.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    program.chmod(0o755)


def _env_with_fake_runners(directory: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
    return env


def _copy_fixture(name: str, root: Path) -> Path:
    destination = root / name
    shutil.copytree(FIXTURES / name, destination)
    return destination


def _copy_ginkgo_suites(root: Path) -> Path:
    """Copy the ginkgo fixture into two differently named suites under one root."""
    for name in ("alpha", "beta"):
        suite = root / name
        shutil.copytree(FIXTURES / "ginkgotiny", suite)
        suite_test = suite / "suite_test.go"
        suite_test.write_text(
            suite_test.read_text(encoding="utf-8")
            .replace("ginkgotiny", f"ginkgo{name}")
            .replace("Ginkgotiny Suite", f"Ginkgotiny {name.capitalize()} Suite"),
            encoding="utf-8",
        )
        go_mod = suite / "go.mod"
        go_mod.write_text(
            go_mod.read_text(encoding="utf-8").replace("example.com/ginkgotiny", f"example.com/ginkgo{name}"),
            encoding="utf-8",
        )
    return root


def _wait_until(check: Callable[[], bool], description: str, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {description}")


def _attempt_dirs(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []
    return sorted(entry for entry in output_dir.iterdir() if entry.is_dir())


def _single_attempt_dir(output_dir: Path) -> Path:
    attempts = _attempt_dirs(output_dir)
    assert len(attempts) == 1
    return attempts[0]


def _read_status(attempt_dir: Path) -> dict[str, object]:
    return json.loads((attempt_dir / STATUS_NAME).read_text(encoding="utf-8"))


def _read_go_events(report_path: Path) -> list[dict[str, object]]:
    events = []
    for raw_line in report_path.read_text(encoding="utf-8").splitlines():
        if raw_line:
            events.append(json.loads(raw_line))
    return events


def _assert_private_attempt(attempt_dir: Path) -> None:
    assert stat.S_IMODE(attempt_dir.stat().st_mode) == 0o700
    for entry in attempt_dir.iterdir():
        assert entry.is_file()
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _processes_matching(needle: bytes) -> list[bytes]:
    matches = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            cmdline = (process_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            matches.append(cmdline)
    return matches


# Python-only CLI and helper contract tests.


def test_parser_keeps_the_command_after_the_separator() -> None:
    args = build_parser().parse_args(["--framework", "go-test", "--output-dir", "/tmp/x", "--", "go", "test", "."])
    assert args.framework == "go-test"
    assert args.command == ["--", "go", "test", "."]


@pytest.mark.parametrize(
    ("framework", "command"),
    [
        ("go-test", []),
        ("go-test", ["go"]),
        ("go-test", ["go", "build", "./..."]),
        ("go-test", ["go", "vet", "./..."]),
        ("go-test", ["make", "test"]),
        ("ginkgo", ["go", "test", "./..."]),
        ("ginkgo", ["ginkgo", "build", "./suite"]),
        ("ginkgo", ["ginkgo", "watch", "./suite"]),
        ("ginkgo", ["ginkgo", "--json-report=x.json", "./suite"]),
        ("ginkgo", ["ginkgo", "--json-report", "x.json", "./suite"]),
        ("ginkgo", ["ginkgo", "--output-dir=/tmp", "./suite"]),
        ("ginkgo", ["ginkgo", "-output-dir=/tmp", "./suite"]),
        ("vitest", ["vitest", "run"]),
    ],
)
def test_validate_command_rejects_unsupported_shapes(framework: str, command: list[str]) -> None:
    with pytest.raises(NativeReportError):
        validate_command(framework=framework, command=command)


@pytest.mark.parametrize(
    ("framework", "command"),
    [
        ("go-test", ["go", "test", "./..."]),
        ("go-test", ["/usr/local/go/bin/go", "test", "-count=1", "."]),
        ("ginkgo", ["ginkgo", "-p", "./suite"]),
        ("ginkgo", ["ginkgo", "run", "-r", "."]),
        ("ginkgo", ["/home/ci/go/bin/ginkgo", "run", "--focus=x", "."]),
    ],
)
def test_validate_command_accepts_supported_shapes(framework: str, command: list[str]) -> None:
    validate_command(framework=framework, command=command)


@pytest.mark.parametrize(
    "command",
    [
        ["go", "test", "-json=false", "."],
        ["go", "test", "-json=0", "."],
        ["go", "test", "--json=FALSE", "."],
        ["go", "test", "-json=banana", "."],
        ["go", "test", "-json", "-json=false", "."],
    ],
)
def test_go_test_json_disabled_or_invalid_is_rejected(command: list[str]) -> None:
    with pytest.raises(NativeReportError):
        validate_command(framework="go-test", command=command)


def test_go_test_json_spellings_and_test_binary_args_are_separated() -> None:
    # -json=true, -json=1, and a bare --json are caller-supplied go flags:
    # accepted as-is, with nothing added.
    for command in (
        ["go", "test", "-json=true", "."],
        ["go", "test", "-json=1", "."],
        ["go", "test", "--json", "."],
        ["go", "test", "-json=false", "-json=true", "."],
    ):
        validate_command(framework="go-test", command=command)
        assert build_go_test_argv(command=command) == command
    # Tokens after -args or -- belong to the test binary, not go test: the
    # helper still adds its own -json in front.
    for command in (["go", "test", ".", "-args", "-json=false"], ["go", "test", ".", "--", "-json"]):
        validate_command(framework="go-test", command=command)
        assert build_go_test_argv(command=command) == ["go", "test", "-json", *command[2:]]


def test_build_go_test_argv_adds_json_without_touching_selection() -> None:
    command = ["go", "test", "-count=1", "-parallel", "2", "-timeout", "30s", "./pkg", "-run", "TestX"]
    assert build_go_test_argv(command=command) == ["go", "test", "-json", *command[2:]]


@pytest.mark.parametrize("flag", ["-run", "--run", "-test.run", "-bench", "-skip"])
def test_json_like_selection_values_are_not_reporting_flags(flag: str) -> None:
    command = ["go", "test", flag, "-json=false", "."]

    validate_command(framework="go-test", command=command)

    assert build_go_test_argv(command=command) == ["go", "test", "-json", *command[2:]]


def test_build_ginkgo_argv_inserts_report_flags_before_caller_args(tmp_path: Path) -> None:
    argv = build_ginkgo_argv(command=["ginkgo", "-p", "./suite"], attempt_dir=tmp_path)
    assert argv == ["ginkgo", f"--json-report={GINKGO_REPORT_NAME}", f"--output-dir={tmp_path}", "-p", "./suite"]


def test_build_ginkgo_argv_puts_flags_after_the_run_subcommand(tmp_path: Path) -> None:
    argv = build_ginkgo_argv(command=["ginkgo", "run", "-r", "."], attempt_dir=tmp_path)
    assert argv == ["ginkgo", "run", f"--json-report={GINKGO_REPORT_NAME}", f"--output-dir={tmp_path}", "-r", "."]


def test_create_attempt_dir_is_unique_within_the_same_second(tmp_path: Path) -> None:
    first = create_attempt_dir(output_dir=tmp_path)
    second = create_attempt_dir(output_dir=tmp_path)
    assert first != second
    assert first.parent == second.parent == tmp_path
    for attempt in (first, second):
        assert re.fullmatch(r"attempt-\d{8}T\d{6}Z-\d+-\d+", attempt.name)
        assert stat.S_IMODE(attempt.stat().st_mode) == 0o700


def test_create_attempt_dir_creates_a_missing_root_as_private(tmp_path: Path) -> None:
    root = tmp_path / "deep" / "reports"
    attempt = create_attempt_dir(output_dir=root)
    assert attempt.parent == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_run_refuses_the_filesystem_root_as_output_dir() -> None:
    with pytest.raises(NativeReportError, match="filesystem root"):
        run_with_native_report(framework="go-test", output_dir=Path("/"), command=["go", "test", "."])


def test_cli_rejects_a_command_from_another_framework_before_creating_files(stable_dir: Path) -> None:
    output_dir = stable_dir / "reports"
    proc = run_helper("go-test", output_dir, ["make", "test"])
    assert proc.returncode == USAGE_ERROR_EXIT
    assert not output_dir.exists()
    assert b"go-test framework expects" in proc.stderr


def test_cli_rejects_an_unknown_framework(stable_dir: Path) -> None:
    proc = run_helper("vitest", stable_dir / "reports", ["vitest", "run"])
    assert proc.returncode == USAGE_ERROR_EXIT


def test_cli_requires_a_command(stable_dir: Path) -> None:
    output_dir = stable_dir / "reports"
    proc = run_helper("go-test", output_dir, [])
    assert proc.returncode == USAGE_ERROR_EXIT
    assert not output_dir.exists()


def test_cli_rejects_an_output_dir_that_is_a_file(stable_dir: Path) -> None:
    blocker = stable_dir / "not-a-dir"
    blocker.write_text("blocked", encoding="utf-8")
    proc = run_helper("go-test", blocker, ["go", "test", "."])
    assert proc.returncode == USAGE_ERROR_EXIT
    assert b"not a directory" in proc.stderr


@pytest.mark.parametrize(
    ("framework", "command", "report_name"),
    [("go-test", ["go", "test", "."], GO_TEST_REPORT_NAME), ("ginkgo", ["ginkgo", "."], GINKGO_REPORT_NAME)],
)
def test_missing_command_is_reported_as_a_gap(
    stable_dir: Path, framework: str, command: list[str], report_name: str
) -> None:
    output_dir = stable_dir / "reports"
    proc = run_helper(framework, output_dir, command, env={"PATH": "/nonexistent"})
    assert proc.returncode == MISSING_COMMAND_EXIT
    assert b"collection error" in proc.stderr
    assert b"not found" in proc.stderr
    attempt = _single_attempt_dir(output_dir)
    status = _read_status(attempt)
    assert status["state"] == "finished"
    assert status["errors"] == [f"test command executable not found: {command[0]}"]
    assert status["exit_code"] == MISSING_COMMAND_EXIT
    assert status["child_returncode"] is None
    report = attempt / report_name
    assert report.exists()
    assert report.stat().st_size == 0


def test_go_test_stream_is_captured_and_the_console_stays_readable(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "go", FAKE_GO_STREAM_SCRIPT)
    output_dir = stable_dir / "reports"
    proc = run_helper("go-test", output_dir, ["go", "test", "./..."], env=_env_with_fake_runners(fake_bin))
    assert proc.returncode == 7
    assert proc.stdout == b"fake console line\nplain text the framework might emit\n"
    assert b'"Action"' not in proc.stdout
    attempt = _single_attempt_dir(output_dir)
    _assert_private_attempt(attempt)
    assert sorted(entry.name for entry in attempt.iterdir()) == sorted(
        [REDACTION_MARKER_NAME, STATUS_NAME, GO_TEST_REPORT_NAME]
    )
    report = attempt / GO_TEST_REPORT_NAME
    assert report.read_bytes() == FAKE_GO_STREAM_REPORT
    status = _read_status(attempt)
    assert status["state"] == "finished"
    assert status["framework"] == "go-test"
    assert status["exit_code"] == 7
    assert status["child_returncode"] == 7
    assert status["child_pid"] is not None
    assert status["interrupted"] is False
    assert status["errors"] == []
    artifact = status["artifacts"][0]
    assert artifact["name"] == GO_TEST_REPORT_NAME
    assert artifact["bytes"] == report.stat().st_size
    assert artifact["json_event_lines"] == 3
    assert artifact["non_json_lines"] == 1
    marker = (attempt / REDACTION_MARKER_NAME).read_text(encoding="utf-8")
    assert "unredacted" in marker
    assert "never uploads" in marker


def test_ginkgo_report_flags_reach_the_runner_and_its_exit_code_is_preserved(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "ginkgo", FAKE_GINKGO_SCRIPT)
    output_dir = stable_dir / "reports"
    proc = run_helper("ginkgo", output_dir, ["ginkgo", "-p", "./suite"], env=_env_with_fake_runners(fake_bin))
    assert proc.returncode == 5
    assert proc.stdout == b"fake ginkgo console\n"
    attempt = _single_attempt_dir(output_dir)
    _assert_private_attempt(attempt)
    report = attempt / GINKGO_REPORT_NAME
    assert report.read_bytes() == FAKE_GINKGO_REPORT
    status = _read_status(attempt)
    assert status["exit_code"] == 5
    assert status["child_returncode"] == 5
    assert status["errors"] == []
    artifact = status["artifacts"][0]
    assert artifact["name"] == GINKGO_REPORT_NAME
    assert artifact["bytes"] == len(FAKE_GINKGO_REPORT)


def test_repeated_attempts_into_one_output_dir_never_collide(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "go", 'echo "attempt output from pid $$"\nexit 0')
    output_dir = stable_dir / "reports"
    env = _env_with_fake_runners(fake_bin)
    first = run_helper("go-test", output_dir, ["go", "test", "."], env=env)
    second = run_helper("go-test", output_dir, ["go", "test", "."], env=env)
    assert first.returncode == 0
    assert second.returncode == 0
    attempts = _attempt_dirs(output_dir)
    assert len(attempts) == 2
    reports = []
    for attempt in attempts:
        _assert_private_attempt(attempt)
        report = attempt / GO_TEST_REPORT_NAME
        reports.append(report.read_bytes())
        assert b"attempt output from pid" in reports[-1]
    assert reports[0] != reports[1]


def test_output_dir_with_dotdot_writes_into_one_resolved_root(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "go", FAKE_GO_STREAM_SCRIPT)
    output_dir = stable_dir / "public" / ".." / "private"
    proc = run_helper("go-test", output_dir, ["go", "test", "."], env=_env_with_fake_runners(fake_bin))
    assert proc.returncode == 7
    assert not (stable_dir / "public").exists()
    attempt = _single_attempt_dir(stable_dir / "private")
    assert (attempt / GO_TEST_REPORT_NAME).read_bytes() == FAKE_GO_STREAM_REPORT


def test_child_restricting_the_attempt_dir_preserves_the_exit_code(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "go", FAKE_GO_LOCK_DIR_SCRIPT)
    output_dir = stable_dir / "reports"
    env = _env_with_fake_runners(fake_bin)
    env["FAKE_GO_DIR"] = str(output_dir)
    proc = run_helper("go-test", output_dir, ["go", "test", "."], env=env)
    assert proc.returncode == 0
    assert b"collection error: status write failed" in proc.stderr
    assert b"Traceback" not in proc.stderr
    attempt = _single_attempt_dir(output_dir)
    # The open report file kept receiving the stream after the directory lock.
    assert (attempt / GO_TEST_REPORT_NAME).read_bytes() == FAKE_GO_LOCK_DIR_REPORT
    status = _read_status(attempt)
    assert status["state"] == "running"  # the final write failed; the run itself succeeded
    assert status["child_pid"] is not None  # persisted as soon as the child started
    assert not _pid_is_alive(status["child_pid"])


def test_console_pipe_loss_does_not_abandon_the_child(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(
        fake_bin,
        "go",
        'printf \'%s\\n\' \'{"Action":"output","Output":"first console line\\n"}\'\n'
        "sleep 2\n"
        'printf \'%s\\n\' \'{"Action":"output","Output":"second console line\\n"}\'\n'
        "exit 3",
    )
    output_dir = stable_dir / "reports"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "--framework",
            "go-test",
            "--output-dir",
            str(output_dir),
            "--",
            "go",
            "test",
            ".",
        ],
        cwd=fake_bin.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env_with_fake_runners(fake_bin),
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline() == b"first console line\n"
        helper.stdout.close()
        helper.wait(timeout=60)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.wait()
        pytest.fail("helper did not exit after the console pipe closed")
    assert helper.returncode == 3
    stderr = helper.stderr.read() if helper.stderr is not None else b""
    assert b"collection error: console stream write failed" in stderr
    assert b"Traceback" not in stderr
    attempt = _single_attempt_dir(output_dir)
    # The private report kept receiving the stream after the console broke.
    assert (attempt / GO_TEST_REPORT_NAME).read_bytes() == (
        b'{"Action":"output","Output":"first console line\\n"}\n{"Action":"output","Output":"second console line\\n"}\n'
    )
    status = _read_status(attempt)
    assert status["exit_code"] == 3
    assert status["errors"] == ["console stream write failed"]
    assert not _pid_is_alive(status["child_pid"])


def test_report_file_limit_preserves_the_child_exit_and_reaps_it(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    event = json.dumps({"Action": "output", "Output": "x" * 1000 + "\n"})
    _write_fake_runner(fake_bin, "go", f"for index in 1 2 3 4 5 6 7 8; do printf '%s\\n' '{event}'; done\nexit 3")
    output_dir = stable_dir / "reports"
    launcher = (
        "import resource, runpy, sys; "
        "resource.setrlimit(resource.RLIMIT_FSIZE, (2048, 2048)); "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            str(HELPER),
            "--framework",
            "go-test",
            "--output-dir",
            str(output_dir),
            "--",
            "go",
            "test",
            ".",
        ],
        capture_output=True,
        env=_env_with_fake_runners(fake_bin),
        timeout=60,
        check=False,
    )

    assert proc.returncode == 3
    assert b"collection error: report stream close failed" in proc.stderr
    assert b"Traceback" not in proc.stderr
    attempt = _single_attempt_dir(output_dir)
    status = _read_status(attempt)
    assert status["exit_code"] == 3
    assert status["child_returncode"] == 3
    assert not _pid_is_alive(status["child_pid"])
    assert (attempt / GO_TEST_REPORT_NAME).stat().st_size <= 2048


def test_deep_json_in_stdout_remains_raw_evidence(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    line = "[" * 2000 + "0" + "]" * 2000
    _write_fake_runner(fake_bin, "go", f"printf '%s\\n' '{line}'\nexit 7")
    output_dir = stable_dir / "reports"

    proc = run_helper("go-test", output_dir, ["go", "test", "."], env=_env_with_fake_runners(fake_bin))

    assert proc.returncode == 7
    assert proc.stdout == (line + "\n").encode()
    attempt = _single_attempt_dir(output_dir)
    assert (attempt / GO_TEST_REPORT_NAME).read_bytes() == proc.stdout
    status = _read_status(attempt)
    assert status["errors"] == []
    assert not _pid_is_alive(status["child_pid"])


def test_interrupt_cleans_descendants_after_the_group_leader_exits(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(
        fake_bin,
        "ginkgo",
        'trap \'exit 0\' TERM\nsh -c \'trap "" TERM INT; echo "$$" > "$FAKE_GINKGO_PID_FILE"; sleep 30\' &\nwait',
    )
    pid_file = stable_dir / "descendant.pid"
    output_dir = stable_dir / "reports"
    environment = _env_with_fake_runners(fake_bin)
    environment["FAKE_GINKGO_PID_FILE"] = str(pid_file)
    helper = subprocess.Popen(
        [sys.executable, str(HELPER), "--framework", "ginkgo", "--output-dir", str(output_dir), "--", "ginkgo", "."],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    try:
        _wait_until(pid_file.exists, "descendant signal handler")
        attempt = _single_attempt_dir(output_dir)
        _wait_until(lambda: _read_status(attempt)["child_pid"] is not None, "persisted child identity")
        child_pid = _read_status(attempt)["child_pid"]
        helper.send_signal(signal.SIGTERM)
        _stdout, stderr = helper.communicate(timeout=30)

        assert helper.returncode == 143
        assert b"Traceback" not in stderr
        status = _read_status(attempt)
        assert status["interrupted"] is True
        assert status["forced_kill"] is True
        assert status["child_returncode"] == 0
        assert not _pid_is_alive(status["child_pid"])
        descendant = Path("/proc") / pid_file.read_text().strip() / "stat"
        try:
            descendant_state = descendant.read_text().split()[2]
        except FileNotFoundError:
            descendant_state = None
        assert descendant_state in (None, "Z")
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child_pid, signal.SIGKILL)
        if helper.poll() is None:
            helper.kill()
            helper.wait()


def test_unexpected_symlink_is_reported_and_never_followed(stable_dir: Path) -> None:
    target = stable_dir / "link-target.txt"
    target.write_text("target content", encoding="utf-8")
    target.chmod(0o644)
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(
        fake_bin,
        "go",
        'printf \'%s\\n\' \'{"Action":"output","Output":"ok\\n"}\'\n'
        'for attempt_dir in "$FAKE_GO_DIR"/*/; do\n'
        '    ln -s "$FAKE_GO_LINK_TARGET" "${attempt_dir}planted-link"\n'
        "done\n"
        "exit 0",
    )
    output_dir = stable_dir / "reports"
    env = _env_with_fake_runners(fake_bin)
    env["FAKE_GO_DIR"] = str(output_dir)
    env["FAKE_GO_LINK_TARGET"] = str(target)
    proc = run_helper("go-test", output_dir, ["go", "test", "."], env=env)
    assert proc.returncode == 0
    assert b"collection error: unexpected symlink in attempt directory" in proc.stderr
    assert b"planted-link" not in proc.stderr
    attempt = _single_attempt_dir(output_dir)
    planted = attempt / "planted-link"
    assert planted.is_symlink()
    assert planted.readlink() == target
    # The sweep must not have chmodded through the symlink.
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    status = _read_status(attempt)
    assert "planted-link" not in {artifact["name"] for artifact in status["artifacts"]}


# Real-runner tests against the tiny fixture suites. The fixtures are copied
# out of the repository first: the repo go.work does not list them, and the
# runners must not write into the repository tree.


@requires_go
def test_go_test_success_preserves_selection_parallelism_and_timeouts(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("gotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GO_BIN, "test", ".", "-run", "TestPassAlways", "-count=1", "-parallel", "2", "-timeout", "60s"]
    proc = run_helper("go-test", output_dir, command, cwd=suite_dir, timeout=180)
    assert proc.returncode == 0
    attempt = _single_attempt_dir(output_dir)
    _assert_private_attempt(attempt)
    events = _read_go_events(attempt / GO_TEST_REPORT_NAME)
    started = {event["Test"] for event in events if event.get("Action") == "run" and "Test" in event}
    passed = {event["Test"] for event in events if event.get("Action") == "pass" and "Test" in event}
    assert started == {"TestPassAlways"}
    assert passed == {"TestPassAlways"}
    assert b"=== RUN   TestPassAlways" in proc.stdout
    assert b"ok  \texample.com/gotiny" in proc.stdout
    status = _read_status(attempt)
    assert status["exit_code"] == 0
    assert status["interrupted"] is False
    assert status["errors"] == []
    artifact = status["artifacts"][0]
    assert artifact["json_event_lines"] == len(events)
    assert artifact["non_json_lines"] == 0


@requires_go
def test_go_test_failure_preserves_exit_code_and_failure_evidence(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("gotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GO_BIN, "test", ".", "-run", "TestFailAlways", "-count=1"]
    proc = run_helper("go-test", output_dir, command, cwd=suite_dir, timeout=180)
    assert proc.returncode == 1
    attempt = _single_attempt_dir(output_dir)
    events = _read_go_events(attempt / GO_TEST_REPORT_NAME)
    failed = {event["Test"] for event in events if event.get("Action") == "fail" and "Test" in event}
    assert failed == {"TestFailAlways"}
    assert b"--- FAIL: TestFailAlways" in proc.stdout
    assert b"this test fails on purpose" in proc.stdout
    status = _read_status(attempt)
    assert status["exit_code"] == 1
    assert status["interrupted"] is False


@requires_go
def test_go_test_accepts_caller_json_flag_spellings(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("gotiny", stable_dir)
    for index, extra in enumerate(["-json", "-json=true"]):
        output_dir = stable_dir / f"reports-{index}"
        command = [GO_BIN, "test", extra, ".", "-run", "TestPassAlways", "-count=1"]
        proc = run_helper("go-test", output_dir, command, cwd=suite_dir, timeout=180)
        assert proc.returncode == 0
        attempt = _single_attempt_dir(output_dir)
        events = _read_go_events(attempt / GO_TEST_REPORT_NAME)
        passed = {event["Test"] for event in events if event.get("Action") == "pass" and "Test" in event}
        assert passed == {"TestPassAlways"}
        assert b"=== RUN   TestPassAlways" in proc.stdout


@requires_go
def test_go_test_json_like_selection_keeps_native_behavior(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("gotiny", stable_dir)
    output_dir = stable_dir / "reports"

    proc = run_helper("go-test", output_dir, [GO_BIN, "test", "-run", "-json", "."], cwd=suite_dir)

    assert proc.returncode == 0
    events = _read_go_events(_single_attempt_dir(output_dir) / GO_TEST_REPORT_NAME)
    assert events[0]["Action"] == "start"
    assert not any(event.get("Action") == "run" for event in events)


@requires_go
def test_go_test_interrupt_keeps_partial_evidence_and_kills_the_child(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("gotiny", stable_dir)
    output_dir = stable_dir / "reports"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "--framework",
            "go-test",
            "--output-dir",
            str(output_dir),
            "--",
            GO_BIN,
            "test",
            ".",
            "-run",
            "TestSlowInterruptible",
            "-count=1",
            "-timeout",
            "10m",
        ],
        cwd=suite_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def report_has_stream() -> bool:
        for attempt in _attempt_dirs(output_dir):
            report = attempt / GO_TEST_REPORT_NAME
            if report.exists() and b"TestSlowInterruptible" in report.read_bytes():
                return True
        return False

    try:
        _wait_until(report_has_stream, "first go test event", timeout_seconds=180)
        helper.send_signal(signal.SIGINT)
        stdout, _stderr = helper.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.communicate()
        pytest.fail("helper did not exit after SIGINT")
    assert helper.returncode == 130
    assert b"=== RUN   TestSlowInterruptible" in stdout
    attempt = _single_attempt_dir(output_dir)
    status = _read_status(attempt)
    assert status["state"] == "finished"
    assert status["interrupted"] is True
    assert status["signal"] == signal.SIGINT
    assert status["exit_code"] == 130
    report = attempt / GO_TEST_REPORT_NAME
    first_line = report.read_bytes().splitlines()[0]
    assert json.loads(first_line)["Action"] == "start"
    assert not _pid_is_alive(status["child_pid"])
    assert _processes_matching(b"TestSlowInterruptible") == []


def test_interrupted_child_that_ignores_signals_is_killed_with_the_first_reason(stable_dir: Path) -> None:
    fake_bin = stable_dir / "fake-bin"
    fake_bin.mkdir()
    _write_fake_runner(fake_bin, "go", FAKE_GO_SIGNAL_TRAP_SCRIPT)
    output_dir = stable_dir / "reports"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "--framework",
            "go-test",
            "--output-dir",
            str(output_dir),
            "--",
            "go",
            "test",
            ".",
        ],
        cwd=fake_bin.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env_with_fake_runners(fake_bin),
    )
    try:
        _wait_until(
            lambda: any((attempt / GO_TEST_REPORT_NAME).exists() for attempt in _attempt_dirs(output_dir)),
            "first fake go event",
        )
        # Before the child exits, the status must show the run in progress;
        # that is the partial-evidence state an interrupted run keeps.
        assert _read_status(_single_attempt_dir(output_dir))["state"] == "running"
        helper.send_signal(signal.SIGINT)
        time.sleep(0.3)
        # A repeated, stronger signal must not replace the first stop reason
        # nor restart the escalation grace.
        helper.send_signal(signal.SIGTERM)
        _stdout, _stderr = helper.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.communicate()
        pytest.fail("helper did not exit after SIGINT")
    assert helper.returncode == 130  # 128 + SIGINT, not 143 for the later SIGTERM
    attempt = _single_attempt_dir(output_dir)
    status = _read_status(attempt)
    assert status["interrupted"] is True
    assert status["signal"] == signal.SIGINT
    assert status["exit_code"] == 130
    assert status["forced_kill"] is True
    assert not _pid_is_alive(status["child_pid"])


@requires_ginkgo
def test_ginkgo_success_preserves_selection_and_parallelism(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("ginkgotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "--focus=passes on purpose", "--procs=2", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suite_dir, timeout=240)
    assert proc.returncode == 0
    attempt = _single_attempt_dir(output_dir)
    _assert_private_attempt(attempt)
    report = json.loads((attempt / GINKGO_REPORT_NAME).read_text(encoding="utf-8"))
    assert isinstance(report, list)
    assert len(report) == 1
    suite = report[0]
    assert suite["SuiteSucceeded"] is True
    passed = [spec["LeafNodeText"] for spec in suite["SpecReports"] if spec["State"] == "passed"]
    skipped = {spec["LeafNodeText"] for spec in suite["SpecReports"] if spec["State"] == "skipped"}
    assert passed == ["passes on purpose"]
    assert skipped == {"fails on purpose", "sleeps for interruption tests"}
    assert b"SUCCESS!" in proc.stdout
    assert b"1 Passed" in proc.stdout
    status = _read_status(attempt)
    assert status["exit_code"] == 0
    assert status["interrupted"] is False
    assert status["errors"] == []


@requires_ginkgo
def test_ginkgo_failure_preserves_exit_code_and_report(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("ginkgotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "--focus=fails on purpose", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suite_dir, timeout=240)
    assert proc.returncode == 1
    attempt = _single_attempt_dir(output_dir)
    report = json.loads((attempt / GINKGO_REPORT_NAME).read_text(encoding="utf-8"))
    suite = report[0]
    assert suite["SuiteSucceeded"] is False
    failed = [spec["LeafNodeText"] for spec in suite["SpecReports"] if spec["State"] == "failed"]
    assert failed == ["fails on purpose"]
    assert b"1 Failed" in proc.stdout
    status = _read_status(attempt)
    assert status["exit_code"] == 1
    assert status["interrupted"] is False


@requires_ginkgo
def test_ginkgo_run_subcommand_success(stable_dir: Path) -> None:
    """The explicit run form that ci-tests.yml uses must keep working."""
    suite_dir = _copy_fixture("ginkgotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "run", "--focus=passes on purpose", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suite_dir, timeout=240)
    assert proc.returncode == 0
    attempt = _single_attempt_dir(output_dir)
    report = json.loads((attempt / GINKGO_REPORT_NAME).read_text(encoding="utf-8"))
    assert report[0]["SuiteSucceeded"] is True
    passed = [spec["LeafNodeText"] for spec in report[0]["SpecReports"] if spec["State"] == "passed"]
    assert passed == ["passes on purpose"]
    assert b"SUCCESS!" in proc.stdout
    status = _read_status(attempt)
    assert status["exit_code"] == 0
    assert status["errors"] == []
    assert status["child_pid"] is not None


@requires_ginkgo
def test_ginkgo_run_subcommand_failure_preserves_exit_code(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("ginkgotiny", stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "run", "--focus=fails on purpose", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suite_dir, timeout=240)
    assert proc.returncode == 1
    attempt = _single_attempt_dir(output_dir)
    report = json.loads((attempt / GINKGO_REPORT_NAME).read_text(encoding="utf-8"))
    assert report[0]["SuiteSucceeded"] is False
    status = _read_status(attempt)
    assert status["exit_code"] == 1


@requires_ginkgo
def test_ginkgo_run_recursive_multi_suite(stable_dir: Path) -> None:
    """Mirror the ci-tests.yml shape: ginkgo run -r from a parent directory."""
    suites_root = _copy_ginkgo_suites(stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "run", "-r", "--focus=passes on purpose", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suites_root, timeout=240)
    assert proc.returncode == 0
    attempt = _single_attempt_dir(output_dir)
    report = json.loads((attempt / GINKGO_REPORT_NAME).read_text(encoding="utf-8"))
    assert {suite["SuiteDescription"] for suite in report} == {"Ginkgotiny Alpha Suite", "Ginkgotiny Beta Suite"}
    for suite in report:
        assert suite["SuiteSucceeded"] is True
    status = _read_status(attempt)
    assert status["exit_code"] == 0
    assert status["errors"] == []


@requires_ginkgo
def test_ginkgo_keep_separate_reports_stay_private(stable_dir: Path) -> None:
    suites_root = _copy_ginkgo_suites(stable_dir)
    output_dir = stable_dir / "reports"
    command = [GINKGO_BIN, "run", "-r", "--keep-separate-reports", "--focus=passes on purpose", "."]
    proc = run_helper("ginkgo", output_dir, command, cwd=suites_root, timeout=240)
    assert proc.returncode == 0
    attempt = _single_attempt_dir(output_dir)
    _assert_private_attempt(attempt)
    separate = sorted(attempt.glob("*_ginkgo-report.json"))
    assert [report.name for report in separate] == ["alpha_ginkgo-report.json", "beta_ginkgo-report.json"]
    for report in separate:
        suite = json.loads(report.read_text(encoding="utf-8"))
        assert len(suite) == 1
        assert suite[0]["SuiteSucceeded"] is True
    # The merged report ginkgo never wrote stays as the empty pre-created file.
    assert (attempt / GINKGO_REPORT_NAME).stat().st_size == 0
    status = _read_status(attempt)
    assert status["errors"] == []
    assert {artifact["name"] for artifact in status["artifacts"]} == {
        "ginkgo-report.json",
        "alpha_ginkgo-report.json",
        "beta_ginkgo-report.json",
    }


@requires_ginkgo
def test_ginkgo_interrupt_kills_the_suite_without_leaking(stable_dir: Path) -> None:
    suite_dir = _copy_fixture("ginkgotiny", stable_dir)
    output_dir = stable_dir / "reports"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "--framework",
            "ginkgo",
            "--output-dir",
            str(output_dir),
            "--",
            GINKGO_BIN,
            "run",
            "--focus=sleeps for interruption tests",
            ".",
        ],
        cwd=suite_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    suite_needle = b"--ginkgo.focus=sleeps for interruption tests"
    try:
        _wait_until(lambda: _processes_matching(suite_needle), "ginkgo suite process", timeout_seconds=240)
        helper.send_signal(signal.SIGINT)
        _stdout, _stderr = helper.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.communicate()
        pytest.fail("helper did not exit after SIGINT")
    assert helper.returncode == 130
    attempt = _single_attempt_dir(output_dir)
    status = _read_status(attempt)
    assert status["state"] == "finished"
    assert status["interrupted"] is True
    assert status["signal"] == signal.SIGINT
    assert status["exit_code"] == 130
    assert not _pid_is_alive(status["child_pid"])
    assert _processes_matching(suite_needle) == []
    report = attempt / GINKGO_REPORT_NAME
    artifact = status["artifacts"][0]
    assert artifact["bytes"] == report.stat().st_size
