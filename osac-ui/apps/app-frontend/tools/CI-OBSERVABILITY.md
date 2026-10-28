# CI observability report capture (Vitest)

`tools/ci-observability-reporter.ts` is an opt-in Vitest reporter that
captures the native JSON test report unchanged, plus a small versioned
diagnostics companion, into a private output directory. It is a
standalone capture producer: nothing in CI loads it yet, and enabling it
in a workflow is a separate, reviewed change.

The captured reports are untrusted producer claims, not
GitHub-authoritative CI results. No canonical test or run ID mapping
exists in this slice; native adapters that interpret these files are a
separately reviewed unit.

## Enable

Load the reporter as a class alongside the default reporter, never
instead of it. Without `outputDir` the reporter is a complete no-op: no
IO, no warnings, no console output, nothing written.

```ts
import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    reporters: [
      'default',
      [resolve('./tools/ci-observability-reporter.ts'), { outputDir: resolve('../ci-reports') }],
    ],
  },
});
```

Vitest loads custom reporters through the module's default export, which
this module provides. The class is also exported by name for tests.

## Output layout

Each reporter instance creates one fresh invocation directory under
`outputDir`:

```
<outputDir>/run-<epoch-ms>-<pid>-<hex>/
  native-report.json   the builtin JSON reporter output, unchanged
  diagnostics.json     companion, schema osac.ci-observability.vitest-native, version 1
  status.json          collection: ok | incomplete | failed, failureLabel, omissions
```

Invocation directories are created with mode 0700 and files with mode
0600, opened exclusively (`wx`): a preexisting name, including a
symlink, is refused instead of overwritten or followed. Names embed a
timestamp, pid, and random suffix, so concurrent runs never collide.

## Privacy rules for the output root

The output root must be private to the current user: owned by the
current uid and mode 0700 (no group or other permission bits). The
reporter creates the root with mode 0700 when missing, and refuses a
root that is a symlink, not a directory, not owned by the current uid,
or readable by group or others. A directory created with a default
umask (0755) is refused.

On SELinux-enforcing hosts the reporter needs no special handling: it
creates every file in place inside the invocation directory and never
renames across directories or writes outside the output root, so files
simply inherit the label of the directory you choose.

## Failure containment

Every IO and serialization error is contained at the reporter boundary:

- The test run's exit code, console output, test selection, and worker
  settings never change. A failing test still fails with exit 1; a
  passing run still passes with exit 0, with capture working and with
  capture faulted alike.
- A capture failure prints exactly one stderr line per run,
  `vitest-ci-observability: collection failed (<label>)`, and records
  the same fixed label in `status.json`. Labels never include paths,
  test names, payloads, or stack traces.
- Companion construction and serialization run inside the same
  containment as the native report generation: malformed SDK data
  becomes a fixed `report-generation` failure, never a native test
  failure and never a fabricated name or timing.
- Bounds: the native report is capped at 64 MiB, diagnostics at
  16 MiB, and observed events at 50 000. An oversize file is omitted
  entirely and named in `status.json` omissions — no placeholder is
  written under the native name, and nothing is truncated or filled
  with invented counts.

Failure labels: `output-dir-prepare`, `output-dir-not-private`,
`invocation-dir-create`, `native-report-write`, `native-report-oversize`,
`diagnostics-write`, `diagnostics-oversize`, `status-write`,
`report-generation`.

## Observation identity

Observations are attached to tests by the public `TestCase.id`, and
hook events carry the public id of their test, suite, or module. Full
names are never used for matching: they repeat across parallel modules
and duplicate concurrent titles inside one module, and a name-keyed
matcher demonstrably swaps arrival times between same-named tests.
Readable names (`fullName`, hook `entity` descriptors) are carried
alongside as private display data only.

`diagnostics.json` exposes the same ids: each test entry has `testId`,
each module entry has its task `id` (plus `moduleId` and
`relativeModuleId`), and each hook event has `entityId`, so every
observation joins the entity it belongs to.

## Observation loss at the memory bound

The reporter retains at most 50 000 result observations and 50 000 hook
observations. The bound is honest loss, not silence:

- `observed.receivedResultCallbacks` and `observed.retainedResultEvents`
  are distinct counts (same for hooks): received is how many public
  callbacks arrived, retained is how many were kept.
- `status.json` becomes `collection: "incomplete"` with
  `droppedObservations` counts and the fixed omission labels
  `result-observations-dropped` and `hook-observations-dropped`.
- Exactly one fixed warning line is printed per run, on the first
  problem; dropped observations never flood the output and never change
  the exit code.
- A test whose result observation was dropped has
  `observedResultAtMs: null` — an absence, never a zero or a fabricated
  arrival.

## What the diagnostics record, and what they cannot

Two kinds of time appear in `diagnostics.json` and they are not the
same fact:

- `diagnostic.*` fields on a test (`duration`, `startTime`,
  `retryCount`) are worker source time, as reported by Vitest.
- `observedResultAtMs` and `hookEvents[].observedAtMs` are
  main-process observation time: when the callback arrived. Callback
  arrival is not execution start or end.

The public Vitest Reporter API provides no worker instance identity, no
per-hook measured durations, and no per-attempt intervals. These stay
unknown: `coverageGaps` records each as the literal string `unknown`,
never a zero or an invented value. If the process is SIGKILLed, nothing
runs and no state exists.

`resultState` is the effective outcome. Vitest reports a `test.todo`
result with state `skipped` and `options.mode` `todo`; the companion
records `todo` so it is not folded into `skipped`.

Hook events cover what the public callbacks report in Vitest 4.1.11:
`beforeEach`/`afterEach` around each running test (entity id joins the
test) and `beforeAll`/`afterAll` for suites and modules (entity id
joins the suite or module). Arrival time only; hook duration stays
unknown.

## Tests

`src/test/observabilityReporter.test.ts` spawns real Vitest 4.1.11
subprocesses against an isolated fixture inside the package. Every case
uses a fresh private output root and compares the exact before/after
set of invocation directories. Coverage:

- No-op mode and full capture shape, for a failing run (exit 1) and a
  passing run (exit 0), including a faulted capture on a passing run.
- Output-root faults: blocked path, non-private mode, symlinked root,
  read-only parent.
- Worker-time versus observed-time as distinct facts.
- Identity regressions: two same-name modules running in parallel with
  controlled finish order, and duplicate concurrent titles in one
  module. An observation attached to the wrong test shows up as an
  arrival before that test's own worker end.
- Observation loss: real runs past the 50 000-callback cap for results
  and hooks, asserting the incomplete status, distinct received and
  retained counts, and exactly one warning.
- Oversize omission: a real 75 000-test run pushes the diagnostics
  companion past its bound; the file is absent, the native report and
  status exist, and the status names the omission.

```bash
cd apps/app-frontend
npx vitest run src/test/observabilityReporter.test.ts
```

Type-check the reporter and the tests separately from the app build:

```bash
cd apps/app-frontend
npx tsc --noEmit -p tools/tsconfig.observability.json
npx tsc --noEmit
```

The dedicated tool tsconfig type-checks the reporter. The workspace ESLint
command covers `src/` files and does not cover `tools/`. Direct ESLint on this
reporter currently fails because its typed-parser projects exclude that path.
Adding tool files to the lint configuration requires a separate reviewed change.
