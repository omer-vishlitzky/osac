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

The helper keeps the reporting command's exit code unchanged:

- `go-test`: inserts `-json` right after `go test`. The raw test2json event
  stream goes to `go-test.json` in the attempt directory. The console keeps a
  readable stream, reconstructed live from the event `Output` fields, so it
  shows the same verbose text a `-v` run prints. Stderr, including build
  errors, passes through untouched and is not captured to a file. An effective
  caller flag that disables `-json` is rejected before the child starts.
  Repeated flags use the last value. Values of selection flags and arguments
  after `-args` or `--` stay with the test binary.
- `ginkgo`: the command line is passed through verbatim; the helper injects
  no reporting flags. Instead it sets `OSAC_CI_GINKGO_REPORT_DIR` to the
  attempt directory in the child's environment. Suites that register the
  component test-support collector publish one native
  `ginkgo-report-<random>.json` final per suite run through a non-failing
  `ReportAfterSuite` hook: the report is built in a private temporary file
  and linked to its final name only after success, so a report IO failure
  never marks the suite failed and never turns passing specs into a failed
  CI run. The helper merges the finals into one `ginkgo-report.json` after
  the child exits. The ginkgo console output is untouched.

Test selection, parallelism, timeouts, environment, and working directory are
passed through verbatim. There are no retests, no timeout changes, and no
output truncation. For Go, every stdout byte reaches the report file and
every `Output` field reaches the console. For Ginkgo, the suites write their
own reports; native content such as spec retry counts and per-repeat runs is
retained because the collector serializes ginkgo's own `Report` through
`reporters.GenerateJSONReport`.

## Ginkgo collector contract

The environment variable `OSAC_CI_GINKGO_REPORT_DIR` is the only activation
switch. The helper overrides any inherited value with the private attempt
directory, so callers cannot redirect reports. When the variable is unset,
suites that register the collector behave exactly as before and add no
reporting node. Collector code lives in component-owned test-support
packages (`fulfillment-service/internal/testing`,
`osac-metering/{schema,metering-service,adapters}/internal/cireports`) and
registers from each suite's `Test` function; the tiny fixture under
`tests/unit/fixtures/ci_observability_native/ginkgotiny/` carries the same
hook for helper tests.

A collector run builds its report in a private exclusive temporary file and
links it to a final name derived from the temporary name's random suffix, so
no run can overwrite another and the suite description stays inside the JSON.
On any collector IO failure the suite result is unchanged, a constant
message is emitted through `GinkgoWriter`, and the temporary file stays
behind as partial evidence. The helper reports a ginkgo attempt that
finished without being interrupted but produced no report evidence as the
collection error `ginkgo left no report evidence; test outcome is unknown`,
whether the exit code is zero or nonzero; this also covers suites that have
not registered the collector, so missing wiring stays visible instead of
silently passing green.

### Collection coverage

`status.json` carries an explicit `coverage` state for ginkgo attempts.
File presence alone never implies that every selected suite ran, so without
a caller manifest the state is always `unknown`. A trusted manifest of
expected suite descriptions can be passed with `--ginkgo-expected-suites
PATH` (one description per line, `#` comments and blank lines ignored); the
helper then compares the manifest with the `SuiteDescription` values parsed
from the published finals and records `complete`, or `incomplete` with the
missing descriptions. A missing expected suite is a source gap, not a test
failure. The helper preserves the child's exit code and reports only the
missing count on stderr. Private status keeps the missing descriptions.
Repeated catalog entries require separate finals with that description.
Suites that fail to compile appear as missing reports too. Rejected files,
leftover partials, interrupted runs and read or write errors leave coverage
`unknown`. Without a catalog it is also `unknown`. W04 records this state
separately from the CI result; incomplete telemetry never fails a test gate.

### Hardened merge and limits

After the child exits the helper merges the finals into one
`ginkgo-report.json`. Finals are opened with `O_NOFOLLOW` and
`O_NONBLOCK` and must be regular files; symlinks are never followed, and a
FIFO, socket, or directory at a final name is rejected without being
opened. Each final is capped at 32 MiB (the contract maximum for a native
file), the totals at 256 MiB and 1000 reports, and directory discovery at
4096 entries. The expected-suite catalog is capped at 128 KiB. JSON depth
is limited to 64; duplicate keys and non-finite numbers are rejected.
Each final contains exactly one suite. The merge keeps the exact validated
bytes, including native timestamp and numeric precision. Read errors use
constant labels without file names or content. The merge is all-or-nothing: any rejection,
leftover partial (`.ginkgo-report-*.json.partial`), or failed write leaves
the merged report unwritten, every final in place as evidence, and the
coverage state `unknown`.

## Private output directory

Every invocation creates a fresh `attempt-<utc-time>-<pid>-<counter>`
directory under `--output-dir`, so attempts never overwrite each other or
unrelated files. The attempt directory is mode 0700, every helper-created
file is mode 0600, and the child runs under umask 0o077 so framework-created
files (including the ginkgo collector finals) are private at
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

- Normal runs exit with the child's exact exit code. An error in the helper's
  own report or status writes never changes that code. The error is printed
  to stderr as `native_reports: collection error: <label>`
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
- A closed or full diagnostic sink also preserves the child's exit code.
  The helper records the diagnostic failure in private status when writable.
- 127 means the command executable was not found; 2 means a usage error.

Interrupted runs keep their partial evidence: the go-test report file is
flushed per line, a ginkgo collector publishes its report during interrupt
handling when the suite gets that far, and `status.json` is written as
`running` before the child starts and
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

- Ginkgo collection depends on suites registering the component collector.
  An unregistered suite still runs and keeps its exit code, but the attempt
  is marked with the missing-evidence collection error. The static
  registration check in `tests/unit/test_ci_native_reports.py` guards the
  wired components; new suites outside them must wire themselves.
- Coverage compares the caller's expected description counts with report
  counts. Reports are producer claims, not proof that trusted code ran.
  Plain Go `Test` functions are outside Ginkgo's reports, including those
  beside a Ginkgo suite. W03 and W04 still need Go test events for them.
- Ginkgo's own `--json-report` or `--output-dir` caller flags are rejected,
  because they reintroduce the fatal reporting path this helper replaces.
- The final file inventory is a snapshot. Descendants that survive a normal
  exit can change files afterwards. A trusted collector validates them again.
- Vitest is not supported. The plan's own coverage table notes the built-in
  Vitest JSON reporter lacks lifecycle and worker fields and needs a small
  reporter owned by the UI component, which is outside this helper's scope.
- The ginkgo path requires a ginkgo v2 CLI and suite binaries; the
  parallel-mode aggregation this relies on was verified with v2.33.1.
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
