/**
 * Opt-in, non-failing native Vitest report capture (W03 producer slice).
 *
 * Wraps the public `JsonReporter` from `vitest/node`. The native JSON stays
 * byte-identical to what the builtin reporter produces; this class only
 * decides where it lands (a private, exclusively created directory) and
 * adds a versioned diagnostics companion built from the public Reporter
 * API. Every IO and serialization error is contained at this boundary: a
 * collection failure is reported as a fixed-label stderr warning and in a
 * best-effort status file, never as a thrown error, an exit-code change,
 * or an aborted test run. Companion construction and serialization run
 * inside the same containment, so malformed SDK data becomes a fixed
 * `report-generation` failure instead of a native test failure.
 *
 * Observations are attached to tests by the public `TestCase.id` (and hook
 * events to their entity's public id), never by display name: full names
 * repeat across modules and duplicate concurrent titles inside one module.
 * Readable names are carried alongside as private data only.
 *
 * The observation memory bound is honest loss, not silence: result and
 * hook callbacks are counted separately as received and retained, dropped
 * observations are recorded as fixed omission labels, the status becomes
 * `incomplete`, and at most one fixed warning line is printed per run.
 *
 * Without a configured `outputDir` the reporter is a complete no-op: no
 * IO, no warnings, no console output, nothing written. The default
 * console reporter is untouched — load this class alongside `default`, it
 * never replaces it.
 *
 * Honest coverage gaps (explicit, never invented): the public API exposes
 * no worker instance identity, no per-hook measured durations, and no
 * per-attempt intervals. `diagnostic()` fields are worker source time;
 * `observed*` fields are main-process hook arrival time. The two are
 * recorded as distinct facts. If the process is SIGKILLed, nothing runs
 * and no state exists — that absence is the gap, named here.
 *
 * Load from a Vitest config as a class (a default-exported object fails
 * at startup):
 *
 *     reporters: ['default', ['./tools/ci-observability-reporter.ts', { outputDir }]]
 */

import { closeSync, lstatSync, mkdirSync, openSync, statSync, writeSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { join } from 'node:path';
import { JsonReporter } from 'vitest/node';
import type {
  ReportedHookContext,
  SerializedError,
  TestCase,
  TestModule,
  TestRunEndReason,
  Vitest,
} from 'vitest/node';

export const CI_OBSERVABILITY_SCHEMA = 'osac.ci-observability.vitest-native';
export const CI_OBSERVABILITY_VERSION = 1;

/** Fixed warning written to stderr when capture fails. The label is a closed set. */
export const CI_OBSERVABILITY_WARNING_PREFIX = 'vitest-ci-observability: collection failed';

const MAX_NATIVE_REPORT_BYTES = 64 * 1024 * 1024;
const MAX_DIAGNOSTICS_BYTES = 16 * 1024 * 1024;
const MAX_OBSERVED_EVENTS = 50_000;

export type CiObservabilityFailureLabel =
  | 'output-dir-prepare'
  | 'output-dir-not-private'
  | 'invocation-dir-create'
  | 'native-report-write'
  | 'native-report-oversize'
  | 'diagnostics-write'
  | 'diagnostics-oversize'
  | 'status-write'
  | 'report-generation';

/** Fixed omission labels for observations dropped at the memory bound. */
export type CiObservabilityOmissionLabel =
  | 'result-observations-dropped'
  | 'hook-observations-dropped';

export interface CiObservabilityReporterOptions {
  /**
   * Private output root for captured reports. Each reporter instance
   * creates one fresh exclusive invocation directory under it. Without
   * this option the reporter is a no-op.
   */
  readonly outputDir?: string;
}

/** One main-process observation of a hook boundary. Arrival time only: the public API carries no measured hook duration. */
interface ObservedHookEvent {
  readonly phase: 'start' | 'end';
  readonly name: 'beforeAll' | 'afterAll' | 'beforeEach' | 'afterEach';
  /** Public id of the hooked test, suite, or module. Identity, not a display name. */
  readonly entityId: string;
  /** Readable private descriptor of the hooked entity. Never used for matching. */
  readonly entity: string;
  readonly observedAtMs: number;
}

/** One main-process observation of a test result arrival. Not worker time. */
interface ObservedResultEvent {
  /** Public TestCase.id. Deterministic identity across name collisions. */
  readonly testId: string;
  readonly observedAtMs: number;
}

interface CompanionTest {
  /** Public TestCase.id, the same identity the hook events and observations carry. */
  readonly testId: string;
  readonly fullName: string;
  /**
   * Effective outcome. Vitest reports a test.todo result with state
   * "skipped" and options.mode "todo"; the mode distinguishes it, so a
   * todo is recorded as "todo", not folded into "skipped".
   */
  readonly resultState: string;
  readonly diagnostic: Readonly<Record<string, unknown>> | null;
  readonly observedResultAtMs: number | null;
}

interface CompanionModule {
  /** Public TestModule task id, the same identity module hook events carry. */
  readonly id: string;
  readonly moduleId: string;
  readonly relativeModuleId: string;
  readonly state: string;
  readonly diagnostic: Readonly<Record<string, unknown>>;
  readonly tests: readonly CompanionTest[];
}

interface Companion {
  readonly schema: string;
  readonly version: number;
  readonly runEndReason: string;
  readonly observed: {
    readonly runEndObservedAtMs: number;
    readonly receivedResultCallbacks: number;
    readonly retainedResultEvents: number;
    readonly receivedHookCallbacks: number;
    readonly retainedHookEvents: number;
  };
  readonly coverageGaps: {
    readonly workerInstanceId: 'unknown';
    readonly perHookDurationMs: 'unknown';
    readonly perAttemptIntervals: 'unknown';
  };
  readonly modules: readonly CompanionModule[];
  readonly hookEvents: readonly ObservedHookEvent[];
}

interface CompanionStatus {
  readonly schema: string;
  readonly version: number;
  readonly collection: 'ok' | 'incomplete' | 'failed';
  readonly failureLabel: string | null;
  readonly omissions: readonly string[];
  readonly droppedObservations: {
    readonly result: number;
    readonly hook: number;
  };
}

const encoder = new TextEncoder();

export class CiObservabilityReporter extends JsonReporter {
  private readonly ciOutputDir: string | undefined;
  private readonly observedHookEvents: ObservedHookEvent[] = [];
  private readonly observedResultEvents: ObservedResultEvent[] = [];
  private readonly omissions: string[] = [];
  private receivedResultCallbacks = 0;
  private receivedHookCallbacks = 0;
  private droppedResultObservations = 0;
  private droppedHookObservations = 0;
  private failureLabel: CiObservabilityFailureLabel | undefined;
  private warningEmitted = false;
  private invocationDir: string | undefined;
  private pendingCompanionText: string | undefined;

  constructor(options: CiObservabilityReporterOptions) {
    // No builtin outputFile: the writeReport override below owns the
    // destination, so the builtin never writes to an unprotected path.
    super({});
    this.ciOutputDir = options.outputDir;
  }

  override onInit(ctx: Vitest): void {
    if (this.ciOutputDir === undefined) {
      return; // No-op mode: not even the base bookkeeping is needed.
    }
    super.onInit(ctx); // the native JSON generation reads ctx and start
  }

  onTestCaseResult(testCase: TestCase): void {
    if (this.ciOutputDir === undefined) {
      return;
    }
    this.receivedResultCallbacks += 1;
    if (this.observedResultEvents.length >= MAX_OBSERVED_EVENTS) {
      this.noteDroppedObservation('result');
      return;
    }
    this.observedResultEvents.push({ testId: testCase.id, observedAtMs: Date.now() });
  }

  onHookStart(hook: ReportedHookContext): void {
    this.observeHook('start', hook);
  }

  onHookEnd(hook: ReportedHookContext): void {
    this.observeHook('end', hook);
  }

  override async onTestRunEnd(
    testModules: ReadonlyArray<TestModule>,
    unhandledErrors?: ReadonlyArray<SerializedError>,
    reason?: TestRunEndReason,
  ): Promise<void> {
    if (this.ciOutputDir === undefined) {
      // No-op: skipping super keeps the builtin from logging the JSON.
      return;
    }
    try {
      // Companion construction and serialization are inside the same
      // containment as the native generation: malformed SDK data becomes
      // a fixed collection failure, never a native test failure.
      this.pendingCompanionText = CiObservabilityReporter.companionText(
        testModules,
        this.observedHookEvents,
        this.observedResultEvents,
        this.receivedResultCallbacks,
        this.receivedHookCallbacks,
        Date.now(),
        reason,
        unhandledErrors?.length ?? 0,
      );
      // Builds the native JSON and calls this.writeReport (the override).
      await super.onTestRunEnd(testModules);
    } catch {
      this.fail('report-generation');
      const dir = this.ensureInvocationDir();
      if (dir !== undefined) {
        this.writeCompanionAndStatus(dir);
      }
    }
  }

  override async writeReport(report: string): Promise<void> {
    if (this.ciOutputDir === undefined) {
      return;
    }
    const dir = this.ensureInvocationDir();
    if (dir === undefined) {
      return;
    }
    this.writeBoundedJson(
      dir,
      'native-report.json',
      report,
      MAX_NATIVE_REPORT_BYTES,
      'native-report-write',
      'native-report-oversize',
    );
    this.writeCompanionAndStatus(dir);
  }

  private observeHook(phase: 'start' | 'end', hook: ReportedHookContext): void {
    if (this.ciOutputDir === undefined) {
      return;
    }
    this.receivedHookCallbacks += 1;
    if (this.observedHookEvents.length >= MAX_OBSERVED_EVENTS) {
      this.noteDroppedObservation('hook');
      return;
    }
    this.observedHookEvents.push({
      phase,
      name: hook.name,
      entityId: hook.entity.id,
      entity: CiObservabilityReporter.describeHookEntity(hook),
      observedAtMs: Date.now(),
    });
  }

  private static describeHookEntity(hook: ReportedHookContext): string {
    if (hook.name === 'beforeEach' || hook.name === 'afterEach') {
      return `test:${hook.entity.fullName}`;
    }
    const entity = hook.entity;
    if (entity.type === 'module') {
      return `module:${entity.moduleId}`;
    }
    return `suite:${entity.name}`;
  }

  private static companionText(
    testModules: ReadonlyArray<TestModule>,
    hookEvents: readonly ObservedHookEvent[],
    resultEvents: readonly ObservedResultEvent[],
    receivedResultCallbacks: number,
    receivedHookCallbacks: number,
    runEndObservedAtMs: number,
    reason: TestRunEndReason | undefined,
    unhandledErrorsCount: number,
  ): string {
    // Match observed result arrivals to tests by the public TestCase.id:
    // full names repeat across modules and duplicate concurrent titles
    // inside one module, so a name can never identify an observation.
    const pendingObservations = new Map<string, number[]>();
    for (const event of resultEvents) {
      const queue = pendingObservations.get(event.testId) ?? [];
      queue.push(event.observedAtMs);
      pendingObservations.set(event.testId, queue);
    }
    const modules: CompanionModule[] = testModules.map((module) => {
      const tests: CompanionTest[] = [];
      for (const testCase of module.children.allTests()) {
        const diagnostic = testCase.diagnostic();
        const queue = pendingObservations.get(testCase.id);
        const observed = queue === undefined || queue.length === 0 ? undefined : queue.shift();
        if (queue !== undefined && queue.length === 0) {
          pendingObservations.delete(testCase.id);
        }
        tests.push({
          testId: testCase.id,
          fullName: testCase.fullName,
          resultState:
            testCase.options.mode === 'todo' && testCase.result().state === 'skipped'
              ? 'todo'
              : testCase.result().state,
          diagnostic: diagnostic === undefined ? null : { ...diagnostic },
          observedResultAtMs: observed ?? null,
        });
      }
      return {
        id: module.id,
        moduleId: module.moduleId,
        relativeModuleId: module.relativeModuleId,
        state: module.state(),
        diagnostic: { ...module.diagnostic() },
        tests,
      };
    });
    const companion: Companion = {
      schema: CI_OBSERVABILITY_SCHEMA,
      version: CI_OBSERVABILITY_VERSION,
      runEndReason: reason ?? 'unknown',
      observed: {
        runEndObservedAtMs,
        receivedResultCallbacks,
        retainedResultEvents: resultEvents.length,
        receivedHookCallbacks,
        retainedHookEvents: hookEvents.length,
      },
      // The public Reporter API provides none of these; they stay unknown.
      coverageGaps: {
        workerInstanceId: 'unknown',
        perHookDurationMs: 'unknown',
        perAttemptIntervals: 'unknown',
      },
      modules,
      hookEvents,
    };
    return JSON.stringify({ ...companion, unhandledErrorsCount });
  }

  private ensureInvocationDir(): string | undefined {
    if (this.invocationDir !== undefined) {
      return this.invocationDir;
    }
    const outputDir = this.ciOutputDir;
    if (outputDir === undefined) {
      return undefined;
    }
    try {
      mkdirSync(outputDir, { recursive: true, mode: 0o700 });
    } catch {
      this.fail('output-dir-prepare');
      return undefined;
    }
    try {
      const metadata = lstatSync(outputDir);
      const details = statSync(outputDir);
      const ownerUid = process.getuid?.();
      if (
        metadata.isSymbolicLink() ||
        !details.isDirectory() ||
        ownerUid === undefined ||
        details.uid !== ownerUid ||
        (details.mode & 0o077) !== 0
      ) {
        this.fail('output-dir-not-private');
        return undefined;
      }
    } catch {
      this.fail('output-dir-not-private');
      return undefined;
    }
    const invocation = join(
      outputDir,
      `run-${Date.now()}-${process.pid}-${randomBytes(4).toString('hex')}`,
    );
    try {
      mkdirSync(invocation, { recursive: false, mode: 0o700 });
    } catch {
      this.fail('invocation-dir-create');
      return undefined;
    }
    this.invocationDir = invocation;
    return invocation;
  }

  private writeCompanionAndStatus(dir: string): void {
    if (this.pendingCompanionText !== undefined) {
      this.writeBoundedJson(
        dir,
        'diagnostics.json',
        this.pendingCompanionText,
        MAX_DIAGNOSTICS_BYTES,
        'diagnostics-write',
        'diagnostics-oversize',
      );
    }
    const status: CompanionStatus = {
      schema: CI_OBSERVABILITY_SCHEMA,
      version: CI_OBSERVABILITY_VERSION,
      collection:
        this.failureLabel !== undefined
          ? 'failed'
          : this.droppedResultObservations + this.droppedHookObservations > 0
            ? 'incomplete'
            : 'ok',
      failureLabel: this.failureLabel ?? null,
      omissions: [...this.omissions],
      droppedObservations: {
        result: this.droppedResultObservations,
        hook: this.droppedHookObservations,
      },
    };
    try {
      CiObservabilityReporter.writeFileExclusive(join(dir, 'status.json'), JSON.stringify(status));
    } catch {
      this.fail('status-write');
    }
  }

  private writeBoundedJson(
    dir: string,
    name: string,
    text: string,
    limit: number,
    writeLabel: CiObservabilityFailureLabel,
    oversizeLabel: CiObservabilityFailureLabel,
  ): void {
    if (encoder.encode(text).length > limit) {
      // Omit the file entirely. No placeholder is written under the
      // native name: a downstream adapter must never mistake a synthetic
      // document for a real native report.
      this.omissions.push(name);
      this.fail(oversizeLabel);
      return;
    }
    try {
      CiObservabilityReporter.writeFileExclusive(join(dir, name), text);
    } catch {
      this.fail(writeLabel);
    }
  }

  private static writeFileExclusive(path: string, text: string): void {
    // 'wx' creates exclusively: a preexisting name (including a symlink)
    // refuses instead of truncating or following, so a foreign entry can
    // never be clobbered.
    const descriptor = openSync(path, 'wx', 0o600);
    try {
      const bytes = encoder.encode(text);
      let offset = 0;
      while (offset < bytes.length) {
        offset += writeSync(descriptor, bytes, offset, bytes.length - offset);
      }
    } finally {
      closeSync(descriptor);
    }
  }

  private noteDroppedObservation(kind: 'result' | 'hook'): void {
    if (kind === 'result') {
      this.droppedResultObservations += 1;
    } else {
      this.droppedHookObservations += 1;
    }
    const label: CiObservabilityOmissionLabel =
      kind === 'result' ? 'result-observations-dropped' : 'hook-observations-dropped';
    if (!this.omissions.includes(label)) {
      this.omissions.push(label);
    }
    // One fixed warning per run, on the first problem: dropped
    // observations must be visible, but never as a warning flood.
    this.warnOnce(label);
  }

  private fail(label: CiObservabilityFailureLabel): void {
    if (this.failureLabel === undefined) {
      this.failureLabel = label;
    }
    this.warnOnce(label);
  }

  private warnOnce(label: string): void {
    if (this.warningEmitted) {
      return;
    }
    this.warningEmitted = true;
    // Fixed label only: no raw paths, stacks, names, or values.
    process.stderr.write(`${CI_OBSERVABILITY_WARNING_PREFIX} (${label})\n`);
  }
}

// Vitest loads custom reporters through the module's default export.
export default CiObservabilityReporter;
