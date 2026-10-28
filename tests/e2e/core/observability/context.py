"""Trusted execution claims and the typed operation context for W08.

Two separate concerns live here:

- ``ExecutionClaims``: the explicit execution, job, suite, and source
  claims a trusted workflow bootstrap hands to the producer. They arrive
  as one JSON document (a file path), never as an environment dump, and
  the producer never invents or enriches them. GitHub owns the
  authoritative CI conclusions; these claims stay attributed producer
  statements until reconciliation.
- ``TestRef`` / ``ResourceRef`` / ``OperationContext``: the small typed
  API shared wait helpers (W08) use to record operations with explicit
  test, resource, and parent links. No callback or factory fields: an
  operation is created from plain values and emits its records through
  the journal bound by the plugin, or no-ops when the plugin is not
  active.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.core.observability.writer import JournalWriter, bounded_text, monotonic_ns

WORKFLOW_EVENTS = frozenset(
    {
        "check_run",
        "check_suite",
        "issue_comment",
        "issues",
        "merge_group",
        "pull_request",
        "pull_request_review",
        "pull_request_target",
        "push",
        "release",
        "repository_dispatch",
        "schedule",
        "workflow_dispatch",
        "workflow_run",
    }
)
COHORTS = frozenset({"pull-request", "merge-queue", "scheduled", "nightly", "manual", "flake-campaign"})
SOURCE_ROLES = frozenset({"tested", "workflow", "test-suite", "installer", "infrastructure", "orchestration"})
FRAMEWORKS = frozenset({"pytest", "ginkgo", "go-test", "vitest", "ansible", "mixed"})
TIERS = frozenset({"unit", "envtest", "component-integration", "contract", "e2e"})
OPERATION_OUTCOMES = frozenset({"passed", "failed", "timeout", "cancelled", "interrupted", "unknown"})

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

MAX_CONTEXT_BYTES = 1024 * 1024


class ContextError(ValueError):
    """The trusted context document is not usable. Messages name fields, never values."""


@dataclass(frozen=True, kw_only=True)
class ExecutionClaims:
    """Copied verbatim into records, never derived or rewritten; GitHub

    reconciliation owns the authoritative binding.
    """

    execution_id: str
    repository_id: int
    repository: str
    workflow_path: str
    workflow_revision: str | None
    run_id: int
    run_attempt: int
    event: str
    cohort: str | None
    job_logical_key: str
    github_job_id: int | None
    job_name: str | None
    suite: Mapping[str, object]
    sources: Sequence[Mapping[str, object]]
    dependencies: Sequence[str] | None = None
    invocation_id: str | None = None

    @classmethod
    def from_json_path(cls, *, path: Path) -> ExecutionClaims:
        return cls.from_mapping(payload=_parse_context_document(raw=_read_context_document(path=path)))

    @classmethod
    def from_mapping(cls, *, payload: Mapping[str, object]) -> ExecutionClaims:
        if not isinstance(payload, Mapping):
            raise ContextError("context-document-not-an-object")
        required = (
            "execution_id",
            "repository_id",
            "repository",
            "workflow",
            "run_id",
            "run_attempt",
            "event",
            "job",
            "suite",
            "sources",
        )
        for key in required:
            if key not in payload:
                raise ContextError(f"context-missing-{key.replace('_', '-')}")
        allowed = set(required) | {"cohort", "dependencies", "invocation_id"}
        if set(payload) - allowed:
            raise ContextError("context-unknown-key")

        workflow = _as_mapping(value=payload["workflow"], label="workflow")
        if "path" not in workflow or "revision" not in workflow:
            raise ContextError("context-missing-workflow-path-or-revision")
        job = _as_mapping(value=payload["job"], label="job")
        for key in ("logical_job_key", "github_job_id"):
            if key not in job:
                raise ContextError(f"context-missing-job-{key.replace('_', '-')}")
        suite = _as_mapping(value=payload["suite"], label="suite")
        for key in ("framework", "tier", "component", "variant"):
            if key not in suite:
                raise ContextError(f"context-missing-suite-{key}")

        event = _as_str(value=payload["event"], label="event")
        if event not in WORKFLOW_EVENTS:
            raise ContextError("context-event-not-allowed")
        cohort = payload.get("cohort")
        if cohort is not None and (not isinstance(cohort, str) or cohort not in COHORTS):
            raise ContextError("context-cohort-not-allowed")
        framework = suite["framework"]
        if not isinstance(framework, str) or framework not in FRAMEWORKS:
            raise ContextError("context-suite-framework-not-allowed")
        tier = suite["tier"]
        if not isinstance(tier, str) or tier not in TIERS:
            raise ContextError("context-suite-tier-not-allowed")

        sources_raw = payload["sources"]
        if not isinstance(sources_raw, list) or not sources_raw:
            raise ContextError("context-sources-empty")
        sources: list[Mapping[str, object]] = []
        roles: set[str] = set()
        for item in sources_raw:
            source = _as_mapping(value=item, label="source")
            for key in ("role", "repository", "revision"):
                if key not in source:
                    raise ContextError(f"context-missing-source-{key}")
            role = _as_str(value=source["role"], label="source-role")
            if role not in SOURCE_ROLES:
                raise ContextError("context-source-role-not-allowed")
            if role in roles:
                raise ContextError("context-source-role-duplicate")
            roles.add(role)
            _as_str(value=source["repository"], label="source-repository")
            revision = source["revision"]
            if revision is not None and not isinstance(revision, str):
                raise ContextError("context-source-revision-invalid")
            sources.append(source)
        for role in ("tested", "workflow"):
            if role not in roles:
                raise ContextError(f"context-source-role-missing-{role}")

        dependencies_raw = payload.get("dependencies")
        if dependencies_raw is not None and not isinstance(dependencies_raw, list):
            raise ContextError("context-dependencies-not-a-list")

        invocation_id = payload.get("invocation_id")
        if invocation_id is not None and (not isinstance(invocation_id, str) or not _ID_PATTERN.match(invocation_id)):
            raise ContextError("context-invocation-id-invalid")
        revision = workflow["revision"]
        if revision is not None and (not isinstance(revision, str) or not _SHA_PATTERN.match(revision)):
            raise ContextError("context-workflow-revision-invalid")
        github_job_id = job["github_job_id"]
        if github_job_id is not None and (not isinstance(github_job_id, int) or isinstance(github_job_id, bool)):
            raise ContextError("context-github-job-id-invalid")
        if isinstance(github_job_id, int) and not isinstance(github_job_id, bool) and github_job_id < 1:
            raise ContextError("context-github-job-id-invalid")
        component = suite["component"]
        if not isinstance(component, str) or not 1 <= len(component) <= 64:
            raise ContextError("context-suite-component-invalid")
        variant = suite["variant"]
        if not isinstance(variant, str) or not 1 <= len(variant) <= 64:
            raise ContextError("context-suite-variant-invalid")
        if dependencies_raw is not None and (
            len(dependencies_raw) > 16 or not all(isinstance(d, str) and 1 <= len(d) <= 64 for d in dependencies_raw)
        ):
            raise ContextError("context-dependencies-invalid")

        return cls(
            execution_id=_as_str(value=payload["execution_id"], label="execution_id"),
            repository_id=_as_int(value=payload["repository_id"], label="repository_id"),
            repository=_as_str(value=payload["repository"], label="repository"),
            workflow_path=_as_str(value=workflow["path"], label="workflow-path"),
            workflow_revision=revision,
            run_id=_as_int(value=payload["run_id"], label="run_id"),
            run_attempt=_as_int(value=payload["run_attempt"], label="run_attempt"),
            event=event,
            cohort=cohort,
            job_logical_key=_as_str(value=job["logical_job_key"], label="job-logical-key"),
            github_job_id=github_job_id,
            job_name=_optional_str(value=job.get("job_name")),
            suite=suite,
            sources=sources,
            dependencies=dependencies_raw,
            invocation_id=invocation_id,
        )

    def execution_payload(self) -> dict[str, object]:
        """The manifest's execution object; echoed by execution_started."""
        job: dict[str, object] = {"logical_job_key": self.job_logical_key, "github_job_id": self.github_job_id}
        if self.job_name is not None:
            job["job_name"] = self.job_name
        execution: dict[str, object] = {
            "execution_id": self.execution_id,
            "repository_id": self.repository_id,
            "repository": self.repository,
            "workflow": {"path": self.workflow_path, "revision": self.workflow_revision},
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "event": self.event,
            "job": job,
        }
        if self.cohort is not None:
            execution["cohort"] = self.cohort
        return execution

    def suite_payload(self) -> dict[str, object]:
        return dict(self.suite)

    def environment_payload(self) -> dict[str, object]:
        return {"sources": [dict(source) for source in self.sources]}


def _as_mapping(*, value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextError(f"context-{label.replace('_', '-')}-not-an-object")
    return value


def _as_str(*, value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContextError(f"context-{label.replace('_', '-')}-invalid")
    return value


def _as_int(*, value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ContextError(f"context-{label.replace('_', '-')}-invalid")
    return value


def _optional_str(*, value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_context_document(*, path: Path) -> bytes:
    """O_NOFOLLOW, O_NONBLOCK, regular-only fstat and a bounded read cap."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ContextError("context-file-unreadable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ContextError("context-file-not-regular")
        if info.st_size > MAX_CONTEXT_BYTES:
            raise ContextError("context-file-oversize")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONTEXT_BYTES:
                raise ContextError("context-file-oversize")
            chunks.append(chunk)
    except OSError:
        raise ContextError("context-file-unreadable") from None
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)
    return b"".join(chunks)


def _reject_constant(name: str) -> object:
    raise ValueError(name)


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


def _parse_context_document(*, raw: bytes) -> Mapping[str, object]:
    """Strictly parse the context document; malformed input is a fixed-label error."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ContextError("context-file-not-utf8") from None
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ContextError("context-file-malformed-json") from None


@dataclass(frozen=True, kw_only=True)
class TestRef:
    """An explicit reference to one test attempt."""

    # Not a pytest test class, despite the name.
    __test__ = False

    test_id: str
    attempt_index: int
    worker_id: str | None = None
    invocation_id: str | None = None

    def payload(self) -> dict[str, object]:
        data: dict[str, object] = {"test_id": self.test_id, "attempt_index": self.attempt_index}
        if self.worker_id is not None:
            data["worker_id"] = self.worker_id
        if self.invocation_id is not None:
            data["invocation_id"] = self.invocation_id
        return data


@dataclass(frozen=True, kw_only=True)
class ResourceRef:
    """A known-safe resource reference: identity is a UID, never a name."""

    kind: str
    uid: str
    name: str | None = None
    api_version: str | None = None
    cluster_uid: str | None = None
    cluster_role: str | None = None
    namespace: str | None = None
    owner_uids: tuple[str, ...] = ()
    tenant: str | None = None
    api_id: str | None = None

    def payload(self) -> dict[str, object]:
        data: dict[str, object] = {"kind": self.kind, "uid": self.uid}
        for key in ("name", "api_version", "cluster_uid", "cluster_role", "namespace", "tenant", "api_id"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.owner_uids:
            data["owner_uids"] = list(self.owner_uids)
        return data


class OperationContext:
    """Inert without an active producer; misuse raises ``ValueError`` as a

    programming error, while journal failures never raise into the test.
    """

    def __init__(
        self,
        *,
        name: str,
        expected_condition: str,
        timeout_ms: int | None = None,
        parent: OperationContext | None = None,
        resource: ResourceRef | None = None,
        test: TestRef | None = None,
        operation_id: str | None = None,
    ) -> None:
        if not name or len(name) > 128:
            raise ValueError("operation name must be 1..128 characters")
        self.name = name
        self.expected_condition = bounded_text(text=expected_condition, limit=256)
        self.timeout_ms = timeout_ms
        self.resource = resource
        self.test = test
        self.parent = parent
        self.operation_id = operation_id or f"op-{uuid.uuid4().hex[:12]}"
        if not _ID_PATTERN.match(self.operation_id):
            raise ValueError("operation id is not filename-safe")
        self._started_mono: int | None = None
        self._finished = False
        # The journal of the invocation this operation started in, or None
        # when it started inert. progress and finish only ever emit into
        # this journal, and only while it is the active one: a handle from
        # an outer invocation must not orphan records into an inner one,
        # and an inert start stays inert for its whole life.
        self._owner: JournalWriter | None = None

    def start(self) -> OperationContext:
        if self._started_mono is not None:
            raise ValueError("operation already started")
        journal = _current_journal()
        if journal is None or _active_operations is None:
            # Inert: no journal is active, so no registry is mutated and
            # no state-dependent validation runs.
            self._started_mono = monotonic_ns()
            return self
        if self.parent is not None and self.parent.operation_id not in _active_operations:
            raise ValueError("parent operation has not started in this invocation")
        if self.operation_id in _active_operations:
            raise ValueError("operation id already used in this invocation")
        self._started_mono = monotonic_ns()
        self._owner = journal
        _active_operations.add(self.operation_id)
        data: dict[str, object] = {
            "operation_id": self.operation_id,
            "name": self.name,
            "expected_condition": self.expected_condition,
        }
        if self.timeout_ms is not None:
            data["timeout_ms"] = self.timeout_ms
        if self.parent is not None:
            data["parent_operation_id"] = self.parent.operation_id
        if self.resource is not None:
            data["resource"] = self.resource.payload()
        if self.test is not None:
            data["test"] = self.test.payload()
        journal.emit(event="operation_started", data=data)
        return self

    def _owner_is_current(self) -> bool:
        """True when the owning invocation's journal is the active one."""
        return self._owner is not None and _current_journal() is self._owner

    def progress(self, *, observed_state: str | None = None, observed_count: int | None = None) -> None:
        if self._finished or self._started_mono is None or not self._owner_is_current():
            return
        journal = self._owner
        data: dict[str, object] = {
            "operation_id": self.operation_id,
            "elapsed_ms": (monotonic_ns() - self._started_mono) // 1_000_000,
        }
        if observed_state is not None:
            data["observed_state"] = bounded_text(text=observed_state, limit=128)
        if observed_count is not None:
            data["observed_count"] = observed_count
        journal.emit(event="operation_progress", data=data)

    def finish(self, *, outcome: str, failure_signature: str | None = None) -> None:
        if self._finished:
            return
        if outcome not in OPERATION_OUTCOMES:
            raise ValueError(f"unknown operation outcome: {outcome}")
        if self._owner is None:
            # Inert from the start: the finish is consumed and the
            # operation stays inert even if a journal becomes active later.
            self._finished = True
            return
        if not self._owner_is_current() or self._started_mono is None:
            # A different invocation is active: the call neither emits
            # into it nor consumes this finish, so the owning invocation
            # can still finish the operation correctly after restore.
            return
        self._finished = True
        data: dict[str, object] = {
            "operation_id": self.operation_id,
            "outcome": outcome,
            "duration_ns": monotonic_ns() - self._started_mono,
        }
        if self.resource is not None:
            data["resource"] = self.resource.payload()
        if self.test is not None:
            data["test"] = self.test.payload()
        if failure_signature:
            data["failure"] = {"signature": bounded_text(text=failure_signature, limit=512)}
        self._owner.emit(event="operation_finished", data=data)

    def __enter__(self) -> OperationContext:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc is None:
            self.finish(outcome="passed")
        elif exc_type is KeyboardInterrupt:
            self.finish(outcome="interrupted")
        else:
            self.finish(outcome="failed", failure_signature=type(exc).__name__)
        return False


# Process-local state installed by the plugin: the journal of the active
# invocation and that invocation's started operation ids, both plain
# values. The plugin binds, suspends, and restores them as a unit, so a
# nested run can neither use the outer journal nor see the outer
# operation ids, and an unbound API is fully inert.
_active_journal: JournalWriter | None = None
_active_operations: set[str] | None = None
_active_test: TestRef | None = None


def _current_journal() -> JournalWriter | None:
    """The active invocation's journal, when it is still usable."""
    if _active_journal is not None and _active_journal.usable:
        return _active_journal
    return None


def _bind_state(*, journal: JournalWriter | None, operations: set[str] | None) -> None:
    """Plugin-internal: install or clear the journal and operation ids."""
    global _active_journal, _active_operations
    _active_journal = journal
    _active_operations = operations


def _set_current_test(*, ref: TestRef | None) -> None:
    """Plugin-internal: track the test attempt currently running."""
    global _active_test
    _active_test = ref


def current_test_ref() -> TestRef | None:
    """The test attempt currently running in this process, or None."""
    return _active_test
