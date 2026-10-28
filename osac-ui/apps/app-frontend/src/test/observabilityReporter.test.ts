/**
 * Real Vitest 4.1.11 subprocess tests for the CI observability reporter.
 *
 * Every case spawns the project's own vitest binary against a tiny
 * isolated fixture inside the package, with the reporter loaded through
 * the public config tuple — the same way CI wiring would load it. The
 * default console, selection, parallelism, and exit codes are never
 * modified; each subprocess run is one single execution (no re-runs
 * inside a capture). Each case uses a fresh private output root and
 * compares the exact before/after set of invocation directories, so a
 * missing or unexpected capture cannot hide behind accumulated runs.
 *
 * Both native exit outcomes are covered: passing fixtures must exit 0
 * with and without capture, including faulted capture, and failing
 * fixtures keep exiting 1. Identity regressions run two same-name
 * modules in parallel with controlled finish order and duplicate
 * concurrent titles in one module: an observation attached to the wrong
 * test shows up as an arrival before that test's own worker end. The
 * observation cap is exercised with real 50 001-callback runs.
 *
 * These are component-level DEV proof only: they prove the reporter
 * boundary (containment, capture shape, identity, honest loss) against
 * real Vitest processes. They do not substitute for the full deployed
 * UI test job.
 */

import { spawnSync } from 'node:child_process';
import {
  chmodSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, expect, test } from 'vitest';

const appRoot = join(dirname(fileURLToPath(import.meta.url)), '../..');
const vitestBin = join(appRoot, 'node_modules', '.bin', 'vitest');
const fixtureDir = join(appRoot, '.ci-observability-reporter-fixture');
const runTimeoutMs = 180_000;

const canary = 'CI-OBS-CANARY-7f3a9c';

// The reporter only writes into a private (0700) root; every case makes
// its own fresh root so captures from other cases can never be mistaken
// for this case's output.
const roots: string[] = [];

const fixtureTests = `
import { beforeEach, expect, test } from 'vitest';

beforeEach(() => {});

test('passing case', () => {
  expect(1 + 1).toBe(2);
});

test('failing case with canary', () => {
  expect('${canary}').toBe('a different value');
});

test.skip('skipped case', () => {
  expect(true).toBe(true);
});

test.todo('todo case');
`;

const passingTests = `
import { expect, test } from 'vitest';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('first passing case', async () => {
  await sleep(50);
  expect(true).toBe(true);
});

test('second passing case', () => {
  expect(2).toBe(2);
});
`;

// Two modules that declare the same public test name and run in
// parallel. The slow module starts a module-level beforeAll so its hook
// events carry the module's public id; the fast module finishes first.
const slowModuleTests = `
import { beforeAll, test } from 'vitest';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

beforeAll(() => {});

test('same public name', async () => {
  await sleep(400);
});
`;

const fastModuleTests = `
import { test } from 'vitest';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('same public name', async () => {
  await sleep(30);
});
`;

// Duplicate titles inside ONE module, declared slow-first and running
// concurrently: the fast test finishes first, so a name-keyed matcher
// attaches the fast arrival to the slow test.
const duplicateModuleTests = `
import { test } from 'vitest';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test.concurrent('duplicate title', async () => {
  await sleep(250);
});

test.concurrent('duplicate title', async () => {
  await sleep(10);
});
`;

const capResultTests = (count: number): string => {
  const lines = ["import { test } from 'vitest';", ''];
  for (let index = 0; index < count; index += 1) {
    lines.push(`test('cap result ${index}', () => {});`);
  }
  return `${lines.join('\n')}\n`;
};

const capHookTests = (count: number): string => {
  const lines = ["import { beforeEach, test } from 'vitest';", '', 'beforeEach(() => {});', ''];
  for (let index = 0; index < count; index += 1) {
    lines.push(`test('cap hook ${index}', () => {});`);
  }
  return `${lines.join('\n')}\n`;
};

// Enough tests to push the diagnostics companion past its 16 MiB bound:
// at roughly 250 bytes per test entry, 75 000 entries exceed it while
// the native report stays far below its own 64 MiB bound.
const oversizeTests = (count: number): string => {
  const lines = ["import { test } from 'vitest';", ''];
  for (let index = 0; index < count; index += 1) {
    lines.push(`test('cap oversize ${index}', () => {});`);
  }
  return `${lines.join('\n')}\n`;
};

const makeRoot = (caseName: string): string => {
  const root = join(tmpdir(), `ci-observability-reporter-test-${process.pid}-${caseName}`);
  rmSync(root, { recursive: true, force: true });
  mkdirSync(root, { recursive: true, mode: 0o700 });
  roots.push(root);
  return root;
};

const reporterTuple = (outputDir?: string): string =>
  outputDir === undefined
    ? "[resolve('../tools/ci-observability-reporter.ts'), {}]"
    : "[resolve('../tools/ci-observability-reporter.ts'), { outputDir: resolve('" +
      outputDir +
      "') }]";

const writeConfig = (name: string, reporters: string, include: string): void => {
  writeFileSync(
    join(fixtureDir, name),
    `import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ${include},
    reporters: ${reporters},
  },
});
`,
    'utf8',
  );
};

const runFixture = (configName: string) =>
  spawnSync(vitestBin, ['run', '--config', configName], {
    cwd: fixtureDir,
    encoding: 'utf8',
    timeout: runTimeoutMs,
    env: { ...process.env },
  });

const runIds = (root: string): string[] =>
  readdirSync(root)
    .filter((entry) => entry.startsWith('run-'))
    .sort();

// Exactly one new invocation directory must appear for a captured run;
// comparing the before/after set catches both a missing capture and an
// unexpected extra one.
const onlyInvocation = (root: string, before: readonly string[]): string => {
  const added = runIds(root).filter((entry) => !before.includes(entry));
  expect(added.length).toBe(1);
  return join(root, added[0]);
};

const readJson = (dir: string, name: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(dir, name), 'utf8')) as Record<string, unknown>;

const reporterWarnings = (stderr: string): string[] =>
  stderr.split('\n').filter((line) => line.startsWith('vitest-ci-observability'));

const diagnosticsOf = (dir: string): Record<string, unknown> => readJson(dir, 'diagnostics.json');

const modulesOf = (diagnostics: Record<string, unknown>): Array<Record<string, unknown>> =>
  diagnostics.modules as Array<Record<string, unknown>>;

const testsOf = (module: Record<string, unknown>): Array<Record<string, unknown>> =>
  module.tests as Array<Record<string, unknown>>;

// The worker-measured end of a test: arrival observations must never
// precede it when they are attached to the right test.
const workerEndOf = (entry: Record<string, unknown>): number => {
  const diagnostic = entry.diagnostic as Record<string, number>;
  return diagnostic.startTime + diagnostic.duration;
};

beforeAll(() => {
  rmSync(fixtureDir, { recursive: true, force: true });
  mkdirSync(fixtureDir, { recursive: true });
  writeFileSync(join(fixtureDir, 'sample.test.ts'), fixtureTests.slice(1), 'utf8');
  writeFileSync(join(fixtureDir, 'passing.test.ts'), passingTests.slice(1), 'utf8');
  writeFileSync(join(fixtureDir, 'a-slow.test.ts'), slowModuleTests.slice(1), 'utf8');
  writeFileSync(join(fixtureDir, 'b-fast.test.ts'), fastModuleTests.slice(1), 'utf8');
  writeFileSync(join(fixtureDir, 'duplicate.test.ts'), duplicateModuleTests.slice(1), 'utf8');
});

afterAll(() => {
  rmSync(fixtureDir, { recursive: true, force: true });
  for (const root of roots) {
    rmSync(root, { recursive: true, force: true });
  }
});

test(
  'without an output directory the reporter is a no-op',
  () => {
    const root = makeRoot('noop-failing');
    writeConfig(
      'vitest.noop-failing.config.ts',
      `['default', ${reporterTuple()}]`,
      "['sample.test.ts']",
    );
    const result = runFixture('vitest.noop-failing.config.ts');
    expect(result.status).toBe(1); // the failing fixture test still fails
    expect(result.stdout).toContain('1 passed');
    expect(result.stdout).toContain('1 failed');
    // No output was created and no warning was emitted.
    expect(reporterWarnings(result.stderr)).toEqual([]);
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'a passing run without an output directory exits zero',
  () => {
    const root = makeRoot('noop-passing');
    writeConfig(
      'vitest.noop-passing.config.ts',
      `['default', ${reporterTuple()}]`,
      "['passing.test.ts']",
    );
    const result = runFixture('vitest.noop-passing.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('2 passed');
    expect(reporterWarnings(result.stderr)).toEqual([]);
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'with an output directory the native report and diagnostics are captured',
  () => {
    const root = makeRoot('enabled-failing');
    writeConfig(
      'vitest.enabled-failing.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['sample.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.enabled-failing.config.ts');
    expect(result.status).toBe(1); // exit code unchanged: the failing test still fails
    expect(result.stdout).toContain('1 passed');
    expect(result.stdout).toContain('1 failed');
    // Successful capture emits no reporter warning at all.
    expect(reporterWarnings(result.stderr)).toEqual([]);

    const dir = onlyInvocation(root, before);
    const native = readJson(dir, 'native-report.json');
    expect(native.success).toBe(false);
    expect(native.numTotalTests).toBe(4);
    expect(native.numPassedTests).toBe(1);
    expect(native.numFailedTests).toBe(1);
    expect(native.numTodoTests).toBe(1);

    const status = readJson(dir, 'status.json');
    expect(status.collection).toBe('ok');
    expect(status.failureLabel).toBeNull();
    expect(status.omissions).toEqual([]);
    expect(status.droppedObservations).toEqual({ result: 0, hook: 0 });

    const diagnostics = diagnosticsOf(dir);
    expect(diagnostics.schema).toBe('osac.ci-observability.vitest-native');
    expect(diagnostics.version).toBe(1);
    expect(diagnostics.runEndReason).toBe('failed');
    expect(diagnostics.coverageGaps).toEqual({
      workerInstanceId: 'unknown',
      perHookDurationMs: 'unknown',
      perAttemptIntervals: 'unknown',
    });
    const observed = diagnostics.observed as Record<string, number>;
    expect(observed.receivedResultCallbacks).toBe(4);
    expect(observed.retainedResultEvents).toBe(4);
    expect(typeof observed.runEndObservedAtMs).toBe('number');
    expect(typeof observed.receivedHookCallbacks).toBe('number');
    expect(typeof observed.retainedHookEvents).toBe('number');

    const modules = modulesOf(diagnostics);
    expect(modules.length).toBe(1);
    const tests = testsOf(modules[0]);
    expect(tests.length).toBe(4);
    // Each test carries its public id; the ids are distinct even where
    // full names would collide.
    const testIds = new Set(tests.map((entry) => entry.testId as string));
    expect(testIds.size).toBe(4);
    for (const entry of tests) {
      expect(typeof entry.testId).toBe('string');
      expect((entry.testId as string).length).toBeGreaterThan(0);
    }
    // Vitest fullNames are bare test names for module-level tests; find
    // by suffix so the lookup does not depend on the module's name.
    const findTest = (suffix: string): Record<string, unknown> => {
      const found = tests.find((entry) => (entry.fullName as string).endsWith(suffix));
      expect(found).toBeDefined();
      return found as Record<string, unknown>;
    };
    const passing = findTest('passing case');
    expect(passing.resultState).toBe('passed');
    const diagnostic = passing.diagnostic as Record<string, unknown> | null;
    expect(diagnostic).not.toBeNull();
    expect(typeof diagnostic?.duration).toBe('number');
    expect(typeof diagnostic?.startTime).toBe('number');
    expect(diagnostic?.retryCount).toBe(0);
    expect(typeof passing.observedResultAtMs).toBe('number');
    expect(findTest('failing case with canary').resultState).toBe('failed');
    expect(findTest('skipped case').resultState).toBe('skipped');
    expect(findTest('todo case').resultState).toBe('todo');
    // Hook observations carry the public id of the test they wrap, so
    // every per-test hook event joins a test in this module.
    const hookEvents = diagnostics.hookEvents as Array<Record<string, unknown>>;
    expect(hookEvents.length).toBeGreaterThan(0);
    for (const event of hookEvents) {
      expect(['beforeEach', 'afterEach']).toContain(event.name);
      expect(testIds.has(event.entityId as string)).toBe(true);
    }
    // The private canary stays private: it is in the captured native
    // report (private local data), never in the reporter's own output.
    // Vitest's own failure display legitimately prints it; the reporter
    // must not add it to any warning or status line.
    expect(reporterWarnings(result.stderr).length).toBe(0);
    expect(result.stdout).not.toContain(canary);
  },
  runTimeoutMs,
);

test(
  'a passing run with capture exits zero',
  () => {
    const root = makeRoot('enabled-passing');
    writeConfig(
      'vitest.enabled-passing.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['passing.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.enabled-passing.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('2 passed');
    expect(reporterWarnings(result.stderr)).toEqual([]);

    const dir = onlyInvocation(root, before);
    const native = readJson(dir, 'native-report.json');
    expect(native.success).toBe(true);
    expect(native.numTotalTests).toBe(2);
    expect(native.numPassedTests).toBe(2);
    expect(native.numFailedTests).toBe(0);

    const status = readJson(dir, 'status.json');
    expect(status.collection).toBe('ok');
    expect(status.failureLabel).toBeNull();

    const diagnostics = diagnosticsOf(dir);
    const modules = modulesOf(diagnostics);
    expect(modules.length).toBe(1);
    const tests = testsOf(modules[0]);
    expect(tests.length).toBe(2);
    for (const entry of tests) {
      expect(entry.resultState).toBe('passed');
      const observed = entry.observedResultAtMs as number;
      expect(typeof observed).toBe('number');
      expect(observed).toBeGreaterThanOrEqual(
        (entry.diagnostic as Record<string, number>).startTime,
      );
    }
  },
  runTimeoutMs,
);

test(
  'a fault in the output directory is contained without changing the failing run',
  () => {
    const root = makeRoot('fault-failing');
    // An existing regular file where the output root should be makes
    // directory preparation fail; the run itself must be unaffected.
    const blockedPath = join(root, 'blocked');
    writeFileSync(blockedPath, 'not a directory', 'utf8');
    writeConfig(
      'vitest.fault-failing.config.ts',
      `['default', ${reporterTuple(blockedPath)}]`,
      "['sample.test.ts']",
    );
    const result = runFixture('vitest.fault-failing.config.ts');
    expect(result.status).toBe(1); // unchanged: the fixture failure decides
    expect(result.stdout).toContain('1 passed');
    // Exactly one fixed warning names the failure, never a flood.
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('collection failed');
    expect(warnings[0]).toContain('output-dir-prepare');
    // The reporter's warning lines are fixed labels; the canary lives in
    // Vitest's own failure display, never in the reporter's output.
    expect(warnings.every((line) => !line.includes(canary))).toBe(true);
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'a fault in the output directory is contained without changing a passing run',
  () => {
    const root = makeRoot('fault-passing');
    const blockedPath = join(root, 'blocked');
    writeFileSync(blockedPath, 'not a directory', 'utf8');
    writeConfig(
      'vitest.fault-passing.config.ts',
      `['default', ${reporterTuple(blockedPath)}]`,
      "['passing.test.ts']",
    );
    const result = runFixture('vitest.fault-passing.config.ts');
    // A faulted capture must not flip a passing run's exit code.
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('2 passed');
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('output-dir-prepare');
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'a non-private output root is refused',
  () => {
    const root = makeRoot('open-root');
    const openRoot = join(root, 'too-open');
    mkdirSync(openRoot, { recursive: true });
    chmodSync(openRoot, 0o755);
    writeConfig(
      'vitest.open.config.ts',
      `['default', ${reporterTuple(openRoot)}]`,
      "['sample.test.ts']",
    );
    const result = runFixture('vitest.open.config.ts');
    expect(result.status).toBe(1);
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('output-dir-not-private');
    // Nothing was captured anywhere near the case root.
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'a symlinked output root is refused',
  () => {
    const root = makeRoot('symlink-root');
    const realRoot = join(root, 'real-root');
    const linkRoot = join(root, 'link-root');
    mkdirSync(realRoot, { recursive: true });
    symlinkSync(realRoot, linkRoot, 'dir');
    writeConfig(
      'vitest.symlink.config.ts',
      `['default', ${reporterTuple(linkRoot)}]`,
      "['sample.test.ts']",
    );
    const result = runFixture('vitest.symlink.config.ts');
    expect(result.status).toBe(1);
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('output-dir-not-private');
    expect(runIds(root)).toEqual([]);
  },
  runTimeoutMs,
);

test(
  'a read-only parent directory is contained',
  () => {
    const root = makeRoot('read-only');
    const readOnlyParent = join(root, 'read-only-parent');
    mkdirSync(readOnlyParent, { recursive: true });
    chmodSync(readOnlyParent, 0o500);
    writeConfig(
      'vitest.blocked.config.ts',
      `['default', ${reporterTuple(join(readOnlyParent, 'reports'))}]`,
      "['sample.test.ts']",
    );
    try {
      const result = runFixture('vitest.blocked.config.ts');
      expect(result.status).toBe(1);
      expect(result.stdout).toContain('1 passed');
      const warnings = reporterWarnings(result.stderr);
      expect(warnings.length).toBe(1);
      expect(warnings[0]).toContain('collection failed');
    } finally {
      chmodSync(readOnlyParent, 0o700);
    }
  },
  runTimeoutMs,
);

test(
  'worker source time and observed arrival time are recorded as distinct facts',
  () => {
    const root = makeRoot('distinct-facts');
    writeConfig(
      'vitest.distinct-facts.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['sample.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.distinct-facts.config.ts');
    expect(result.status).toBe(1);
    const diagnostics = diagnosticsOf(onlyInvocation(root, before));
    const tests = testsOf(modulesOf(diagnostics)[0]);
    const passing = tests.find((entry) =>
      (entry.fullName as string).endsWith('passing case'),
    ) as Record<string, unknown>;
    const diagnostic = passing.diagnostic as Record<string, number>;
    // diagnostic.startTime is the worker-measured test start; the observed
    // arrival is a main-process fact. Both must exist; the coverage gaps
    // name what the public API does not provide.
    expect(diagnostic.startTime).toBeGreaterThan(0);
    expect(passing.observedResultAtMs as number).toBeGreaterThanOrEqual(diagnostic.startTime);
    expect(diagnostics.coverageGaps).toEqual({
      workerInstanceId: 'unknown',
      perHookDurationMs: 'unknown',
      perAttemptIntervals: 'unknown',
    });
  },
  runTimeoutMs,
);

test(
  'observations stay with their tests across same-name parallel modules',
  () => {
    const root = makeRoot('same-name');
    writeConfig(
      'vitest.same-name.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['a-slow.test.ts', 'b-fast.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.same-name.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('2 passed');
    expect(reporterWarnings(result.stderr)).toEqual([]);

    const diagnostics = diagnosticsOf(onlyInvocation(root, before));
    const modules = modulesOf(diagnostics);
    expect(modules.length).toBe(2);
    const byModule = new Map(modules.map((module) => [module.relativeModuleId as string, module]));
    const slowModule = byModule.get('a-slow.test.ts') as Record<string, unknown>;
    const fastModule = byModule.get('b-fast.test.ts') as Record<string, unknown>;
    expect(slowModule).toBeDefined();
    expect(fastModule).toBeDefined();
    const slow = testsOf(slowModule)[0];
    const fast = testsOf(fastModule)[0];
    // Same public display name, distinct public identities.
    expect(slow.fullName).toBe('same public name');
    expect(fast.fullName).toBe('same public name');
    expect(slow.testId).not.toBe(fast.testId);

    // An observation attached to the wrong test arrives before that
    // test's own worker end: with these durations a swap shows up as a
    // delta near -370 ms on the slow test. Correct attachment keeps
    // every arrival after its own worker end.
    const slowDelta = (slow.observedResultAtMs as number) - workerEndOf(slow);
    const fastDelta = (fast.observedResultAtMs as number) - workerEndOf(fast);
    expect(slowDelta).toBeGreaterThan(-250);
    expect(fastDelta).toBeGreaterThan(-250);
    expect(slowDelta).toBeLessThan(1500);
    expect(fastDelta).toBeLessThan(1500);
    // Arrivals follow completion order through one callback channel.
    expect(fast.observedResultAtMs as number).toBeLessThanOrEqual(
      slow.observedResultAtMs as number,
    );

    // The slow module's beforeAll observations carry the module's
    // public id, so hook events join the module they belong to.
    const hookEvents = diagnostics.hookEvents as Array<Record<string, unknown>>;
    const moduleHooks = hookEvents.filter(
      (event) => event.name === 'beforeAll' || event.name === 'afterAll',
    );
    expect(moduleHooks.length).toBe(2);
    for (const event of moduleHooks) {
      expect(event.entityId).toBe(slowModule.id);
      expect(event.entity as string).toContain('module:');
    }
  },
  runTimeoutMs,
);

test(
  'duplicate concurrent titles in one module keep their own observations',
  () => {
    const root = makeRoot('duplicate-title');
    writeConfig(
      'vitest.duplicate.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['duplicate.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.duplicate.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('2 passed');
    expect(reporterWarnings(result.stderr)).toEqual([]);

    const diagnostics = diagnosticsOf(onlyInvocation(root, before));
    const modules = modulesOf(diagnostics);
    expect(modules.length).toBe(1);
    const tests = testsOf(modules[0]);
    expect(tests.length).toBe(2);
    // Duplicate display names, distinct public identities.
    expect(tests[0].fullName).toBe('duplicate title');
    expect(tests[1].fullName).toBe('duplicate title');
    expect(tests[0].testId).not.toBe(tests[1].testId);

    // The declared-slow test (250 ms) is the one with the longer
    // measured duration; the declared-fast one finishes first.
    const sorted = [...tests].sort(
      (a, b) =>
        (b.diagnostic as Record<string, number>).duration -
        (a.diagnostic as Record<string, number>).duration,
    );
    const slow = sorted[0];
    const fast = sorted[1];
    expect((slow.diagnostic as Record<string, number>).duration).toBeGreaterThan(100);
    expect((fast.diagnostic as Record<string, number>).duration).toBeLessThan(100);
    // A name-keyed matcher attaches the fast arrival to the slow test
    // (a delta near -240 ms here); identity-keyed attachment keeps both
    // arrivals after their own worker ends.
    expect((slow.observedResultAtMs as number) - workerEndOf(slow)).toBeGreaterThan(-100);
    expect((fast.observedResultAtMs as number) - workerEndOf(fast)).toBeGreaterThan(-100);
    expect(fast.observedResultAtMs as number).toBeLessThan(slow.observedResultAtMs as number);
  },
  runTimeoutMs,
);

test(
  'the result observation cap is reported as honest loss',
  () => {
    writeFileSync(join(fixtureDir, 'cap-results.test.ts'), capResultTests(50005), 'utf8');
    const root = makeRoot('cap-results');
    writeConfig(
      'vitest.cap-results.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['cap-results.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.cap-results.config.ts');
    // Dropped observations never fail the run.
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('50005 passed');
    // One fixed warning line, not one per dropped event.
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('result-observations-dropped');

    const dir = onlyInvocation(root, before);
    const status = readJson(dir, 'status.json');
    expect(status.collection).toBe('incomplete');
    expect(status.failureLabel).toBeNull();
    expect(status.omissions).toContain('result-observations-dropped');
    expect(status.droppedObservations).toEqual({ result: 5, hook: 0 });

    const diagnostics = diagnosticsOf(dir);
    const observed = diagnostics.observed as Record<string, number>;
    expect(observed.receivedResultCallbacks).toBe(50005);
    expect(observed.retainedResultEvents).toBe(50000);
    expect(observed.receivedHookCallbacks).toBe(0);
    expect(observed.retainedHookEvents).toBe(0);
    expect(typeof observed.runEndObservedAtMs).toBe('number');
    const tests = testsOf(modulesOf(diagnostics)[0]);
    expect(tests.length).toBe(50005);
    // Exactly the dropped observations are absent, named as null.
    expect(tests.filter((entry) => entry.observedResultAtMs === null).length).toBe(5);
  },
  runTimeoutMs,
);

test(
  'the hook observation cap is reported as honest loss',
  () => {
    writeFileSync(join(fixtureDir, 'cap-hooks.test.ts'), capHookTests(25001), 'utf8');
    const root = makeRoot('cap-hooks');
    writeConfig(
      'vitest.cap-hooks.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['cap-hooks.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.cap-hooks.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('25001 passed');
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('hook-observations-dropped');

    const dir = onlyInvocation(root, before);
    const status = readJson(dir, 'status.json');
    expect(status.collection).toBe('incomplete');
    expect(status.failureLabel).toBeNull();
    expect(status.omissions).toContain('hook-observations-dropped');
    expect(status.droppedObservations).toEqual({ result: 0, hook: 2 });

    const diagnostics = diagnosticsOf(dir);
    // Result observations are separate from hook loss: every test kept
    // its own arrival.
    const observed = diagnostics.observed as Record<string, number>;
    expect(observed.receivedResultCallbacks).toBe(25001);
    expect(observed.retainedResultEvents).toBe(25001);
    expect(observed.receivedHookCallbacks).toBe(50002);
    expect(observed.retainedHookEvents).toBe(50000);
    expect(typeof observed.runEndObservedAtMs).toBe('number');
    const tests = testsOf(modulesOf(diagnostics)[0]);
    expect(tests.length).toBe(25001);
    expect(tests.every((entry) => entry.observedResultAtMs !== null)).toBe(true);
    // Retained hook events still join their tests by public id.
    const testIds = new Set(tests.map((entry) => entry.testId as string));
    const hookEvents = diagnostics.hookEvents as Array<Record<string, unknown>>;
    expect(hookEvents.length).toBe(50000);
    for (const event of hookEvents) {
      expect(testIds.has(event.entityId as string)).toBe(true);
    }
  },
  runTimeoutMs,
);

test(
  'an oversize diagnostics file is omitted, never faked',
  () => {
    writeFileSync(join(fixtureDir, 'cap-oversize.test.ts'), oversizeTests(75000), 'utf8');
    const root = makeRoot('oversize');
    writeConfig(
      'vitest.oversize.config.ts',
      `['default', ${reporterTuple(root)}]`,
      "['cap-oversize.test.ts']",
    );
    const before = runIds(root);
    const result = runFixture('vitest.oversize.config.ts');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('75000 passed');
    // The run also drops result observations past the cap; the first
    // problem prints the single warning line.
    const warnings = reporterWarnings(result.stderr);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain('result-observations-dropped');

    const dir = onlyInvocation(root, before);
    // The oversize diagnostics file is absent — no placeholder under
    // the real name — while the native report and status exist.
    const written = readdirSync(dir).sort();
    expect(written).toEqual(['native-report.json', 'status.json']);

    const status = readJson(dir, 'status.json');
    expect(status.collection).toBe('failed');
    expect(status.failureLabel).toBe('diagnostics-oversize');
    expect(status.omissions).toContain('diagnostics.json');
    expect(status.omissions).toContain('result-observations-dropped');
    expect(status.droppedObservations).toEqual({ result: 25000, hook: 0 });

    const native = readJson(dir, 'native-report.json');
    expect(native.success).toBe(true);
    expect(native.numTotalTests).toBe(75000);
  },
  runTimeoutMs,
);
