# CI observability native reports

`native_reports.py` runs one trusted CI test command and keeps the test
framework's own native report file in a private output directory. It is local
tooling for the OSAC CI observability work: it preserves framework reports and
writes local bookkeeping only. It does not produce CI observability contract
events, and it never uploads anything.

## Usage

```
python tools/ci_observability/native_reports.py \
    --framework go-test \
    --output-dir /tmp/native-reports \
    -- go test ./osac-operator/... -run TestReconcile -count=1 -parallel 4

python tools/ci_observability/native_reports.py \
    --framework ginkgo \
    --output-dir /tmp/native-reports \
    -- ginkgo run -r --timeout 1h internal
```

Pass the command after `--` so its own flags are not parsed by the helper.
Both the bare `ginkgo [flags] [suites]` form and the explicit
`ginkgo run [flags] [suites]` form used by `.github/ci-tests.yml` are
supported.

## What is preserved

The helper adds only the framework's reporting flag and changes nothing else:

- `go-test`: inserts `-json` right after `go test`. The raw test2json event
  stream goes to `go-test.json` in the attempt directory. The console keeps a
  readable stream, reconstructed live from the event `Output` fields, so it
  shows the same verbose text a `-v` run prints. Stderr, including build
  errors, passes through untouched and is not captured to a file. A caller
  effective flag that disables `-json` is rejected before the child starts.
  Repeated flags use the last value. Values of selection flags and arguments
  after `-args` or `--` stay with the test binary.
- `ginkgo`: inserts `--json-report=ginkgo-report.json` and
  `--output-dir=<attempt dir>` directly after `ginkgo` or `ginkgo run`,
  because ginkgo requires the subcommand before its flags and rejects flags
  after suite paths. Multiple suites merge into one report file, one array
  element per suite; with the caller's `--keep-separate-reports`, ginkgo
  writes one `<suite>_ginkgo-report.json` per suite. The ginkgo console
  output is untouched.

Test selection, parallelism, timeouts, environment, and working directory are
passed through verbatim. There are no retests, no timeout changes, and no
output truncation: every byte of the child's stdout reaches the report file,
and every `Output` field reaches the console.

## Private output directory

Every invocation creates a fresh `attempt-<utc-time>-<pid>-<counter>`
directory under `--output-dir`, so attempts never overwrite each other or
unrelated files. The attempt directory is mode 0700, every helper-created
file is mode 0600, and the child runs under umask 0o077 so framework-created
files (including ginkgo's `--keep-separate-reports` output) are private at
creation. A final sweep re-asserts private modes; unexpected symlinks inside
the attempt directory are never followed and are reported as collection
errors instead.

The output directory is resolved before use; a path containing `..` writes
into the resolved single root. If the directory does not exist it is created
with mode 0700. The helper refuses an output directory that is the filesystem
root or an existing non-directory.

The reports are raw framework output and may contain secrets, tenant names,
host names, or paths. They stay local. The helper never uploads them; they
must not be uploaded or attached anywhere without the OSAC-approved redaction
process.

## Exit codes, interrupts, and collection errors

- Normal runs exit with the child's exact exit code. A collection problem
  (report or status IO failure, unexpected symlink) never changes it: the
  error is printed to stderr as `native_reports: collection error: <label>`
  and recorded in `status.json` when that file is still writable.
- If the helper receives SIGINT or SIGTERM, it forwards the signal to the
  child's whole process group (the child runs in its own session) and exits
  with 128 plus the first received signal number. Repeated signals are
  forwarded but do not replace the first stop reason or restart the grace
  period. A child that traps the signal gets 10 seconds to exit before the
  group is killed. If its leader exits first, the helper kills remaining
  descendants before returning. The status file records a forced kill.
- If the helper's console or report stream breaks mid-run, it keeps draining
  the child, reaps it, and preserves its exit code; the broken sink is
  reported as a collection error.
- 127 means the command executable was not found; 2 means a usage error.

Interrupted runs keep their partial evidence: the report file is flushed per
line, and `status.json` is written as `running` before the child starts and
again with the child pid as soon as the child exists. A helper that is killed
outright (SIGKILL) leaves that `running` status in place, so consumers can
tell an incomplete attempt from a finished one. Note the limit: SIGKILL of
the helper itself cannot run the signal forwarding or escalation, so the
child process group keeps running until an external supervisor or
reconciliation handles it; the persisted `running` status with the child pid
is what makes such a loss visible.

`status.json` records the framework, timestamps, child pid and return code,
interrupt state, artifact names with sizes, and collection errors. It
deliberately records no command line or environment. It is explicitly not
the CI observability data contract; that contract is owned elsewhere.

## Limitations

- Vitest is not supported. The plan's own coverage table notes the built-in
  Vitest JSON reporter lacks lifecycle and worker fields and needs a small
  reporter owned by the UI component, which is outside this helper's scope.
- The ginkgo path requires a ginkgo v2 CLI with `--json-report` and
  `--output-dir`, and rejects caller commands that already set those flags.
- Ginkgo subcommands other than the run forms (`build`, `watch`, ...) are
  rejected, because they do not run specs.
- Go test stderr is not captured to a file; it streams to the console only.

## Tests

From the repository root:

```
uv run pytest -n 0 tests/unit/test_ci_native_reports.py
```

The real-runner tests use the tiny Go and Ginkgo fixtures under
`tests/unit/fixtures/ci_observability_native/` and skip visibly when the `go`
or `ginkgo` executable is unavailable. The remaining tests use fake runner
scripts and cover the CLI contract, collection-error paths, and interrupt
handling without a Go toolchain.
