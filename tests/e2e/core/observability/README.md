# CI observability pytest producer

Explicit-opt-in pytest journaling for the OSAC CI observability work
(work package W02). It writes private, per-process JSON Lines journals
that satisfy the frozen CI observability contract version 1
(`monitoring/ci_observability/contracts` in osac-test-infra, commit
`c2cbeb5`). This is a producer only: no ingestion, no workflow wiring, no
upload, and no redaction claim.

## Opt-in

Load the plugin explicitly and pass both a private output directory and a
trusted context document:

```
pytest -p tests.e2e.core.observability.plugin \
    --ci-observability-output=<private dir> \
    --ci-observability-context=<path to context JSON>
```

The same values can come from `OSAC_CI_OBS_OUTPUT` and
`OSAC_CI_OBS_CONTEXT`. Loading the plugin without both values is a no-op:
selection, workers, exit codes, reports, and finalizer order are
untouched. A partially or wrongly configured plugin prints one warning to
stderr and stays inactive rather than failing the run.

The context document is written by the trusted workflow bootstrap
(`ExecutionClaims.from_json_path` in `context.py` validates its shape).
It carries the execution, job, suite, and source claims explicitly; the
producer never reads environment dumps and never invents identity. The
numeric GitHub job id stays null unless the trusted bootstrap bound it.

## Run layout and record ownership

```
<output>/                 0700 run root
  bundle/                 0700 the delivered bundle
    journals/             0700 one JSON Lines file per process, 0600
    native-report/        declared native report copies (footer)
    bundle-manifest.json  written last, by the footer
  state/                  0700 transient producer state, never delivered
```

Ownership is split so a workflow that already writes its own job header
never gets a duplicate:

- The trusted bootstrap (W04 in the workflows, or
  `writer.write_job_header` / `JobJournal.finish` for a standalone
  harness) owns `journals/controller.jsonl`: exactly one
  `execution_started` before the measured processes start (W04 writes it
  before installation) and one `execution_finished` plus the bundle
  manifest at the end.
- The plugin owns one journal per pytest invocation
  (`journals/<invocation_id>.jsonl`, written by the controller or serial
  main process) plus one journal per xdist worker
  (`journals/<invocation_id>-gw<N>.jsonl`, written by that worker).
  Records: `invocation_started` / `invocation_finished`, worker
  lifecycle, `collection_finished` and `test_collected` (exactly one
  definition per invocation), setup/call/teardown phases with native
  outcomes, and fixture setup, teardown, and use edges.

One job execution (one `execution_id`, one header) may run several
invocations; each pytest command line is one invocation with its own
`invocation_id`, and all of its xdist workers share that id. Under xdist
the controller does not collect: the single collection definition is
emitted by the controller from the workers' real collection reports and
per-worker metadata sidecars under `state/`. Worker loss is recorded by
the controller as a `worker_stopped` with outcome `interrupted`; phases
of a lost worker stay dangling in that worker's journal and are never
fabricated.

Worker identity is derived only from xdist's `config.workerinput`.
`PYTEST_XDIST_WORKER` is inherited by every subprocess a worker spawns,
so trusting it would misclassify a serial pytest child started inside a
worker; such a child is a normal serial invocation of its own. A real
worker whose `workerinput` lacks a usable worker id never gets a
fabricated serial-main identity either: its records carry the explicit
`unknown` marker and a counted `worker-id-unavailable` gap in the
writer status and the manifest's completeness errors.

## Nested in-process runs

A nested `pytest.main()` inside a running session is suspended per
config: the outer tracker, its journal, the W08 context bindings, the
invocation environment variable, and the `FixtureDef.finish` class
wrapper all survive the inner run (the wrapper is reference-counted, and
it only records fixtures whose request belongs to the tracker's own
config, so an inner run that never loaded this plugin cannot pollute the
outer journal either).

- An inner run with its own output and context activates its own tracker
  and records itself as a separate invocation of the same job.
- An inner run without them stays unrecorded, and the outer journal gets
  an explicit gap: a terminal `collection_state` record with source
  `pytest-nested-invocation`, status `unavailable`, a counted dropped
  record, and a `nested-invocation-unrecorded` collection error, so the
  bundle never claims healthy completeness while records are omitted.
- Each inner `pytest_unconfigure` restores exactly the tracker it
  suspended (a per-config map, not a single global slot), so three-level
  nesting restores the right tracker at every level. The outer exit
  status and invocation records are never replaced by inner ones.

Known residual: a nested run that does not load this plugin runs no
producer hooks, but if its own code calls the W08 operation API, those
operation records still land in the outer journal, attributed to the
outer test that caused the nested run. The outer invocation's
duplicate-id and unknown-parent validation applies to those helper
calls as well.

## Measurement rules

- Starts are written immediately; sequence numbers, UTC wall-clock
  timestamps, per-process monotonic clocks, and a per-journal `clock_id`
  are on every record.
- A journal write that fails mid-run (the tests use a `RLIMIT_FSIZE`
  fault boundary with `SIGXFSZ` ignored, which needs no privileges) is
  counted, its partially written line is truncated away, and the file
  offset is repositioned, so the journal stays valid JSON Lines and a
  transient failure that later recovers appends at the right offset
  instead of leaving a sparse hole. Records are written through the raw
  descriptor precisely because a buffered writer would resurrect a
  dropped line's bytes on recovery. When the truncate recovery itself
  fails, the journal is permanently disabled with a counted
  `journal-truncate-failed` gap: nothing is written after it and no torn
  line is buried. The native exit code and JUnit are unchanged and the
  loss is a visible collection gap. A privileged ENOSPC mount is not
  exercised and remains a named gap.
- Items another `pytest_collection_modifyitems` hook adds to the session
  are part of the collected set and its counts, because they run.
- Native report copies are exclusive: the target is created with
  `O_EXCL|O_NOFOLLOW` mode 0600, so a symlink or an existing file at the
  target is refused with a fixed-label error and nothing outside the
  bundle is ever written. An unpersisted manifest fails `finish`
  loudly instead of returning a bundle that was never written.
- Fixture setup and finalizer work are measured as separate intervals;
  the idle lifetime between them is never charged as cost. Finalizers a
  fixture factory registers during the test body are covered because
  they run inside `FixtureDef.finish`. Parameterized fixture instances
  get distinct instance ids and hashed parameter fingerprints; raw
  parameter values and their `repr` are never recorded. A fixture
  definition's identity is its root-relative namespace, module, and
  qualified name, so a parent and a child conftest defining the same
  argname stay two definitions while one cached definition stays one
  instance. The parameter fingerprint hashes the argument name with the
  value's position and the parameter count — never the value contents —
  and pytest delivers `@pytest.mark.parametrize` values through a fresh
  per-mark fixture, so the fingerprint alone cannot tell two marks
  apart: each actual setup of a definition identity (a real cache miss,
  including those per-mark param fixtures) takes the next setup ordinal
  for that identity, and the ordinal is hashed into the instance id.
  One cached definition keeps ordinal 1 for its single setup lifetime;
  independently parametrized tests sharing an argument name and index
  get distinct lifetime ids.
- Test ids carry parameter values only as safe hashes: the parameter
  payload opened by the first `[` after the first `::` (the path/name
  boundary; both separators can appear inside a parameter value, so
  first-match wins structurally) is replaced whole by
  `[p:<sha256 of the payload, first 12 hex>]` at every journal, sidecar,
  status, operation-ref, and selection boundary. The mapping is
  deterministic, so ids join across records, runs, and interpreters, and
  distinct values stay distinct. Native JUnit and the console keep the
  raw ids. Raw `-k`/`-m` selector expressions are recorded the same way,
  as a hash identity with membership and counts only.
- A malformed or hostile context document (JSON `null`, non-object
  roots, unhashable enum members, 50000-deep nesting, non-finite
  numbers, duplicate keys, oversize files, FIFOs or symlinks at the
  path) never reaches the run: the document is read through a
  non-following, non-blocking, size-capped descriptor, parsed strictly,
  and every failure is a fixed-label error that leaves the plugin
  inactive with one stderr warning and no content echo.
- Failure payloads carry exception class names only (member class names
  for exception groups). No environment, command line, argument,
  traceback, or request body is ever recorded.
- Every writer IO or serialization failure is a visible collection gap
  (dropped-record counts, fixed-label error notes, per-writer status
  files), and never changes the native run: exit codes, selection,
  worker counts, deadlines, finalizer order, and exceptions are
  preserved exactly.
- Journals are private and unredacted until the approved redaction and
  delivery path (W06) runs. The manifest states
  `redaction.applied: false`.

## Operation context API (for W08 helpers)

`context.py` exports the small typed API shared wait helpers use:

```python
from tests.e2e.core.observability.context import OperationContext, ResourceRef, TestRef, current_test_ref

with OperationContext(
    name="wait_for_provision",
    expected_condition="ComputeInstance phase Ready",
    timeout_ms=1_200_000,
    resource=ResourceRef(kind="ComputeInstance", uid=cr_uid, api_id=api_id),
    test=current_test_ref(),
) as operation:
    operation.progress(observed_state="Provisioning")
```

`TestRef` / `ResourceRef` are plain frozen keyword-only dataclasses with
explicit invocation and parent links; `OperationContext` emits
`operation_started` / `operation_progress` / `operation_finished`
through the journal of the invocation that started it and is fully inert
without one: no registry is mutated and no state-dependent validation
runs. Operation ids live in a per-invocation registry the plugin binds,
suspends, and restores with the journal, so duplicates and unknown
parents raise only while active, and a nested run can never use an
outer operation as a parent or cross-link the outer invocation. An
operation started under one invocation emits nothing into another: calls
from a nested or restored context neither emit nor consume the finish,
an inert start stays inert after activation, and progress after a
terminal finish records nothing. Unknown outcomes raise `ValueError` (a
programming error), while journal failures never raise into the test.

## Private pytest API compatibility

Verified against the two provided versions, pytest 8.3.5 and 9.1.1:

- `_pytest.fixtures.FixtureDef.finish` is wrapped at class level only
  while the plugin is active. The wrapper measures around the original
  call; finalizers keep their exact LIFO order and nothing moves. The
  wrap is reference-counted and removed when the last tracker shuts
  down, and it only records fixtures whose request config matches the
  tracker's own pytest config.
- `FixtureDef.cached_result` is read to skip no-op `finish` calls
  (pytest re-calls `finish` after a parameter-driven cache swap).
- `FixtureDef.baseid` is read as the fixture's stable, root-relative
  namespace, digested from its normalized raw form: a directory named
  ``parent.child`` and a directory ``parent/child`` have identical
  dotted display paths but different raw baseids, so the digest keeps
  them distinct. A root-level definition (empty or ``.`` baseid,
  depending on the pytest version) is a legitimate unprefixed identity.
  No absolute checkout root, source line, repr, or process-memory
  identity is used.
- `FixtureRequest._fixture_defs` is read passively after each test's
  teardown to emit `fixture_used` edges; nothing is executed.
- `_pytest.skipping.xfailed_key` is read to classify a strict unexpected
  pass: both pytest versions report it as a plain failure without
  `wasxfail`, and the evaluated xfail state on the item is the native
  evidence. When the signal is unavailable on a future version, the
  outcome stays the native one.
- `pytest_fixture_setup` fires once per real (cache-miss) setup in both
  versions.
- xdist hooks `pytest_configure_node`, `pytest_testnodedown`, and
  `pytest_xdist_node_collection_finished` are public xdist plugin API,
  registered as optional hooks so the plugin loads without xdist.
  `config.workerinput` (set by xdist before the worker's hooks run) is
  the only worker signal.

## Tests

Real pytest subprocess tests live in
`tests/unit/test_ci_pytest_observability.py` with tiny fixture projects
under `tests/unit/fixtures/ci_pytest_observability/` (named `case_*.py`
so the repository's own suite never collects them; the directory's
`conftest.py` hooks are inert without their opt-in env). The test oracle
imports the production contract validators from the frozen checkout,
and the matrix covers serial and `-n 4 --dist loadfile` sessions (a
session worker's children inherit `PYTEST_XDIST_WORKER`, which the
producer must not trust), nested in-process runs two and three levels
deep, strict and conditional xfail classification on both pytest
versions, phase boundaries measured without report-generation latency,
fixture-definition overrides, selector-expression privacy, malformed
sidecars, metadata hardlink and FIFO plants, and a mid-journal write
fault boundary:

```
OSAC_CI_OBS_CONTRACT_DIR=<infra>/monitoring/ci_observability/contracts \
OSAC_CI_OBS_PYTEST_PYTHON=<pytest 9.1.1 + xdist interpreter> \
<python with jsonschema and pytest-xdist> -m pytest -o addopts="" \
    tests/unit/test_ci_pytest_observability.py
```

Drop `-o addopts=""` to run the same module under the repository default
`-n 4 --dist loadfile`. `OSAC_CI_OBS_CONTRACT_DIR` must name the
contracts directory of the pinned osac-test-infra checkout; when set but
unusable the tests fail, and when unset they skip with an explicit
reason. `OSAC_CI_OBS_PYTEST_PYTHON` names an alternate child interpreter
(for example the repository venv with pytest 9.1.1 and pytest-xdist);
without it the child runs use the test session's own interpreter.
