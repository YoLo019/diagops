# DiagOps V9 Durable & Observable Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** `approved`

**Goal:** Build a durable, observable, multi-session SRE Agent runtime with bounded concurrency, explicit cancellation and recovery, reconnectable events, OpenTelemetry, audit replay, and structured Run Diff while preserving V8.2 RCA behavior.

**Architecture:** Add a domain-specific Runtime layer above the existing Diagnosis pipeline. A lease-owning `RuntimeCoordinator` drives stable phases through an async `PhaseExecutor`; workers perform bounded external I/O while one writer atomically persists business results, ordered events, and checkpoints. SQLite and memory stores implement the same runtime contract, and additive APIs expose background Runs, SSE, recovery, replay, and diff without removing existing synchronous Investigation APIs.

**Tech Stack:** Python 3.11, FastAPI lifespan and `StreamingResponse`, Pydantic v2, SQLAlchemy 2, SQLite, asyncio, OpenTelemetry Python SDK with OTLP/HTTP, pytest, React, TypeScript, Vite, TanStack Query.

**Approved spec:** `docs/superpowers/specs/2026-07-17-diagops-v9-durable-observable-runtime-design.md`

**Implementation baseline:** `origin/codex/mvp-backend@3f06d9ae87729185b3429c839f11cc366de75000` or a verified descendant containing the complete V8.2 implementation.

---

## Execution Preconditions

1. Do not implement V9 in the current dirty workspace at `3831305`. At execution time, use `using-git-worktrees` to create an isolated worktree from the V8.2 baseline and use a `codex/` branch.
2. Preserve every user change in the current workspace. Copy only this approved spec and approved plan into the isolated worktree if they are not already reachable from its base.
3. V8.2's real 40-case OpenRCA result is a release gate, not a prerequisite for writing key-free V9 code. Freeze its run ID, model, prompt version, scores, and artifact checksums before declaring V9 complete.
4. Do not commit, push, merge, or create a PR unless the user separately authorizes that Git action.
5. Keep production integrations read-only. V9 adds no Shell, SSH, arbitrary URL, arbitrary PromQL, infrastructure mutation, external queue, or automatic remediation path.

## File Map

### New backend files

- `backend/domain/runtime.py`: public Runtime enums and Pydantic contracts.
- `backend/runtime/__init__.py`: Runtime package exports.
- `backend/runtime/store.py`: `RuntimeStore` protocol and memory implementation.
- `backend/runtime/sqlite_store.py`: SQLite Runtime persistence, leases, ordered events, and atomic phase commits.
- `backend/runtime/writer.py`: single-writer command queue and lease-fenced persistence façade.
- `backend/runtime/phases.py`: phase inputs, outputs, deterministic digests, and phase ordering.
- `backend/runtime/phase_executor.py`: async adapter over the existing Diagnosis components.
- `backend/runtime/coordinator.py`: Run/Attempt lifecycle, checkpoints, cancellation, and manual resume.
- `backend/runtime/manager.py`: process-wide bounded Run scheduler and task ownership.
- `backend/runtime/event_hub.py`: per-Run live notification fan-out used after durable persistence.
- `backend/runtime/telemetry.py`: allowlisted OTel tracing and optional OTLP exporter.
- `backend/runtime/replay.py`: zero-external-call audit replay and `ReplayReport` generation.
- `backend/runtime/diff.py`: deterministic structured Run comparison.
- `backend/runtime/faults.py`: test-only named fault injection points with production-disabled default.
- `backend/api/runtime_runs.py`: additive Runtime Run, event, SSE, cancel, resume, replay, and diff endpoints.
- `backend/services/runtime_acceptance.py`: concurrency, recovery, replay, privacy, and performance acceptance runner.

### New frontend files

- `frontend/src/RuntimeWorkbench.tsx`: Run selection, phase timeline, swimlanes, controls, details, replay, and diff.
- `frontend/src/useRuntimeEvents.ts`: reconnectable EventSource hook with sequence deduplication.
- `frontend/src/runtimeProjection.ts`: stable event-to-timeline projection.

### New tests

- `tests/domain/test_runtime_models.py`
- `tests/db/test_runtime_migrations.py`
- `tests/runtime/test_memory_store.py`
- `tests/runtime/test_sqlite_store.py`
- `tests/runtime/test_writer.py`
- `tests/runtime/test_phase_executor.py`
- `tests/runtime/test_coordinator.py`
- `tests/runtime/test_manager.py`
- `tests/runtime/test_tool_idempotence.py`
- `tests/runtime/test_event_hub.py`
- `tests/runtime/test_telemetry.py`
- `tests/runtime/test_replay.py`
- `tests/runtime/test_diff.py`
- `tests/runtime/test_fault_injection.py`
- `tests/api/test_runtime_runs_api.py`
- `tests/api/test_runtime_sse.py`
- `tests/services/test_runtime_acceptance.py`
- `tests/frontend/test_runtime_workbench.py`

### Existing files to modify

- `pyproject.toml`, `uv.lock`: add OTel SDK and OTLP/HTTP exporter.
- `config/diagops.yaml`, `backend/config/settings.py`, `tests/config/test_settings.py`: typed Runtime configuration and environment overrides.
- `backend/db/schema.py`, `backend/db/migrations.py`, `backend/db/session.py`, `tests/db/test_migrations.py`: schema V6 and foreign-key validation.
- `backend/db/repositories.py`, `backend/db/sqlite_repository.py`: connection-aware business persistence used by atomic Runtime commits.
- `backend/db/models.py`: expose `runtime_available` on existing Investigation responses with a safe default.
- `backend/domain/tool_calls.py`: additive logical call, idempotency, execution, and Runtime Run identity.
- `backend/diagnosis/adaptive_tools.py`, `backend/diagnosis/agents_runtime.py`: tool result resolver/sink and cancellable model boundaries.
- `backend/diagnosis/orchestrator.py`: compatibility façade over extracted phases.
- `backend/services/container.py`, `backend/main.py`: construct Runtime services and manage startup/shutdown audit.
- `backend/api/investigations.py`, `backend/api/events.py`: route new analyses through Runtime while preserving synchronous responses.
- `backend/benchmarks/openrca/runner.py`, `tests/benchmarks/test_openrca_runner.py`: record Runtime Run IDs and preserve paired evaluation behavior.
- `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`: Runtime API types and workbench integration.
- `tests/api/test_investigations_api.py`, `tests/api/test_events_api.py`, `tests/diagnosis/test_orchestrator.py`, `tests/diagnosis/test_agents_runtime.py`, `tests/diagnosis/test_adaptive_tools.py`: compatibility and regression coverage.
- `README.md`, `docs/superpowers/current.md`: V9 configuration, operations, migration, API, and iteration status.

## Milestone 1: Contracts and Durable Storage

### Task 1: Activate the Approved V9 Baseline

**Files:**
- Modify: `docs/superpowers/current.md`
- Verify: `docs/superpowers/specs/2026-07-17-diagops-v9-durable-observable-runtime-design.md`
- Verify: `docs/superpowers/plans/2026-07-17-diagops-v9-durable-observable-runtime-implementation-plan.md`

- [ ] **Step 1: Verify the isolated worktree baseline**

Run:

```powershell
git rev-parse HEAD
git merge-base --is-ancestor 3f06d9ae87729185b3429c839f11cc366de75000 HEAD
git status --short
```

Expected: the ancestor command exits `0`, and only the approved V9 documents are new or modified.

- [ ] **Step 2: Update the current iteration pointer**

Set these exact fields in `docs/superpowers/current.md` while preserving its routing-only role:

```text
Version: V9
Iteration status: ready
Spec status: approved
Plan status: approved
Implementation status: not_started
Completion commit: none
Verification evidence: none
Blocker: none
```

Link the approved V9 spec and plan. Record V8.2 as the implemented code baseline at `3f06d9a`, while keeping the real OpenRCA 40-case result as an independent release gate if its frozen evidence is not yet present.

- [ ] **Step 3: Verify routing consistency**

Run:

```powershell
rg -n "Version: V9|Spec status: approved|Plan status: approved|Implementation status: not_started|2026-07-17-diagops-v9" docs/superpowers/current.md
```

Expected: all V9 status fields and both authoritative document paths are printed once.

### Task 2: Add Runtime Domain Contracts and Typed Configuration

**Files:**
- Create: `backend/domain/runtime.py`
- Modify: `backend/config/settings.py`
- Modify: `config/diagops.yaml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/domain/test_runtime_models.py`
- Test: `tests/config/test_settings.py`

- [ ] **Step 1: Write failing contract tests**

Cover these exact rules in `tests/domain/test_runtime_models.py`:

```python
def test_runtime_event_rejects_non_allowlisted_payload_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.RUN_CREATED,
            actor_type=RuntimeActorType.RUNTIME,
            safe_payload={"prompt": "secret"},
        )


def test_checkpoint_contains_control_state_not_business_payload() -> None:
    checkpoint = RuntimeCheckpoint(
        run_id="run-1",
        attempt_id="attempt-1",
        completed_phase=RuntimePhase.EVIDENCE_COLLECTION,
        event_sequence=4,
        state_digest="a" * 64,
        resume_state=RuntimeResumeState(
            completed_evidence_ids=["evidence-1"],
            remaining_tool_budget=3,
            successful_tool_keys=["tool-key-1"],
        ),
    )
    assert "evidence" not in checkpoint.resume_state.model_dump()
```

Also test all approved Run transitions, terminal-state rejection, positive finite limits, OTel default-off behavior, and JSON rejection of NaN/Infinity.

- [ ] **Step 2: Run the tests and confirm missing contracts**

Run:

```powershell
uv run pytest tests/domain/test_runtime_models.py tests/config/test_settings.py -v
```

Expected: failure because Runtime contracts and settings do not exist.

- [ ] **Step 3: Implement the exact public contract**

Define string enums and models in `backend/domain/runtime.py`:

```python
class RuntimeRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CANCELLING = "cancelling"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimePhase(StrEnum):
    INTAKE = "intake"
    EVIDENCE_COLLECTION = "evidence_collection"
    DETERMINISTIC_RCA = "deterministic_rca"
    SPECIALIST_ANALYSIS = "specialist_analysis"
    CONFLICT_REVIEW = "conflict_review"
    COORDINATION = "coordination"
    REPORT_GENERATION = "report_generation"
    FINALIZE = "finalize"


ALLOWED_TRANSITIONS: dict[RuntimeRunStatus, frozenset[RuntimeRunStatus]] = {
    RuntimeRunStatus.CREATED: frozenset(
        {RuntimeRunStatus.RUNNING, RuntimeRunStatus.CANCELLED}
    ),
    RuntimeRunStatus.RUNNING: frozenset(
        {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLING,
            RuntimeRunStatus.INTERRUPTED,
        }
    ),
    RuntimeRunStatus.CANCELLING: frozenset(
        {RuntimeRunStatus.CANCELLED, RuntimeRunStatus.INTERRUPTED}
    ),
    RuntimeRunStatus.INTERRUPTED: frozenset({RuntimeRunStatus.RUNNING}),
    RuntimeRunStatus.COMPLETED: frozenset(),
    RuntimeRunStatus.FAILED: frozenset(),
    RuntimeRunStatus.CANCELLED: frozenset(),
}
```

Add `RuntimeRunKind`, `RuntimeRunReason`, `RuntimeAttemptStatus`, `RuntimeActorType`, every approved `RuntimeEventType`, and runtime failure categories. Implement `RuntimeRun`, `RuntimeAttempt`, `RuntimeEvent`, `RuntimeCheckpoint`, `RuntimeResumeState`, `ReplayReport`, and `RuntimeRunDiff` with the approved fields. Validate `safe_payload` against an event-type allowlist and recursively reject prohibited keys and non-finite floats.

- [ ] **Step 4: Add Runtime settings and dependencies**

Add:

```python
class OpenTelemetrySettings(BaseModel):
    enabled: bool = False
    endpoint: str | None = None


class RuntimeSettings(BaseModel):
    enabled: bool = True
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_parallel_steps_per_run: int = Field(default=3, ge=1, le=16)
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    heartbeat_seconds: int = Field(default=10, ge=1, le=300)
    opentelemetry: OpenTelemetrySettings = Field(default_factory=OpenTelemetrySettings)
```

Add `runtime: RuntimeSettings` to `AppSettings`, the approved YAML block, and environment overrides:

```text
DIAGOPS_RUNTIME_ENABLED
DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS
DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN
DIAGOPS_RUNTIME_LEASE_SECONDS
DIAGOPS_RUNTIME_HEARTBEAT_SECONDS
DIAGOPS_RUNTIME_OTEL_ENABLED
DIAGOPS_RUNTIME_OTEL_ENDPOINT
```

Add `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` to project dependencies and refresh `uv.lock` with `uv lock`.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
uv run pytest tests/domain/test_runtime_models.py tests/config/test_settings.py -v
uv run ruff check backend/domain/runtime.py backend/config/settings.py tests/domain/test_runtime_models.py tests/config/test_settings.py
```

Expected: all focused tests pass and Ruff reports no errors.

### Task 3: Add Schema V6 and Preserve Historical Databases

**Files:**
- Modify: `backend/db/schema.py`
- Modify: `backend/db/migrations.py`
- Modify: `backend/db/session.py`
- Test: `tests/db/test_runtime_migrations.py`
- Test: `tests/db/test_migrations.py`

- [ ] **Step 1: Write migration tests**

Create V5 fixtures and assert upgrade to V6 creates exactly four Runtime tables, preserves old rows, leaves old Investigations without Runtime history, and passes `PRAGMA foreign_key_check`:

```python
def test_v5_database_migrates_to_v6_without_synthetic_runs(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v5.db'}")
    create_v5_fixture(engine, investigation_id="inv-history")

    initialize_database(engine)

    with engine.connect() as connection:
        assert connection.execute(select(schema_version.c.version)).scalar_one() == 6
        assert connection.execute(select(runtime_runs)).all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
```

Test event `(run_id, sequence)` uniqueness, Attempt number uniqueness, foreign keys, parent/source Run references, lease expiry index, and the partial unique index that allows only one active live Run per Investigation.

- [ ] **Step 2: Run migration tests and confirm V6 is absent**

Run:

```powershell
uv run pytest tests/db/test_runtime_migrations.py tests/db/test_migrations.py -v
```

Expected: failures because V6 tables and migration do not exist.

- [ ] **Step 3: Define the four tables**

Add `runtime_runs`, `runtime_attempts`, `runtime_events`, and `runtime_checkpoints`. Store public model fields in typed columns where they participate in filtering or integrity checks. Store `safe_payload`, `resume_state`, and ID arrays as JSON. Add internal `next_event_sequence` to `runtime_runs` and nullable internal `trace_id`/`root_span_id` columns to `runtime_attempts`; these are persistence metadata and are not exposed by the public Runtime models or APIs.

Use this partial uniqueness condition:

```python
Index(
    "uq_runtime_runs_one_active_live_per_investigation",
    runtime_runs.c.investigation_id,
    unique=True,
    sqlite_where=and_(
        runtime_runs.c.run_kind == "live",
        runtime_runs.c.status.in_(("created", "running", "cancelling", "interrupted")),
    ),
)
```

Add `UNIQUE(run_id, sequence)` and `UNIQUE(run_id, attempt_number)`, plus indexes for Run history, event catch-up, and expired leases.

- [ ] **Step 4: Implement V5-to-V6 migration**

Set `CURRENT_SCHEMA_VERSION = 6`, register `migrate_v5_to_v6`, extend the physical manifest, and keep strict rejection of claimed-but-corrupt schemas. Do not insert Runtime rows for historical Investigations.

- [ ] **Step 5: Verify migrations**

Run:

```powershell
uv run pytest tests/db/test_runtime_migrations.py tests/db/test_migrations.py -v
uv run ruff check backend/db/schema.py backend/db/migrations.py tests/db/test_runtime_migrations.py tests/db/test_migrations.py
```

Expected: V3, V4, V5, and fresh databases reach V6; corruption tests and foreign-key tests pass.

### Task 4: Implement Memory and SQLite Runtime Stores

**Files:**
- Create: `backend/runtime/__init__.py`
- Create: `backend/runtime/store.py`
- Create: `backend/runtime/sqlite_store.py`
- Modify: `backend/db/repositories.py`
- Modify: `backend/db/sqlite_repository.py`
- Test: `tests/runtime/test_memory_store.py`
- Test: `tests/runtime/test_sqlite_store.py`

- [ ] **Step 1: Write one shared store contract suite**

Parametrize the same tests over `InMemoryRuntimeStore` and `SQLiteRuntimeStore`. Assert create/get/list, newest-first history, strict state transitions, one active live Run, Attempt numbering, lease acquire/renew/fencing, ordered events, event pagination, cancellation request, expired lease interruption, checkpoint lookup, and cross-Investigation rejection.

Use a race test:

```python
def test_only_one_resume_acquires_interrupted_run(runtime_store) -> None:
    run = interrupted_run(runtime_store)
    barrier = Barrier(2)

    def acquire(owner: str) -> bool:
        barrier.wait()
        return runtime_store.acquire_lease(
            run.id,
            owner=owner,
            expected_status=RuntimeRunStatus.INTERRUPTED,
        ) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ("worker-a", "worker-b")))
    assert sorted(results) == [False, True]
```

- [ ] **Step 2: Run tests and confirm stores are missing**

Run:

```powershell
uv run pytest tests/runtime/test_memory_store.py tests/runtime/test_sqlite_store.py -v
```

Expected: import failures for the Runtime stores.

- [ ] **Step 3: Define the store protocol**

The protocol must expose concrete, lease-fenced operations:

```python
class RuntimeStore(Protocol):
    def create_run(self, run: RuntimeRun) -> RuntimeRun: ...
    def get_run(self, run_id: str) -> RuntimeRun: ...
    def list_runs(self, investigation_id: str) -> list[RuntimeRun]: ...
    def create_attempt(self, attempt: RuntimeAttempt) -> RuntimeAttempt: ...
    def acquire_lease(
        self, run_id: str, *, owner: str, expected_status: RuntimeRunStatus
    ) -> RuntimeRun | None: ...
    def renew_lease(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> RuntimeRun | None: ...
    def transition_run(
        self,
        run_id: str,
        *,
        expected: RuntimeRunStatus,
        target: RuntimeRunStatus,
        owner: str | None = None,
        lease_version: int | None = None,
    ) -> RuntimeRun: ...
    def request_cancel(self, run_id: str) -> RuntimeRun: ...
    def append_event(self, event: RuntimeEvent) -> RuntimeEvent: ...
    def list_events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[RuntimeEvent]: ...
    def get_checkpoint(self, checkpoint_id: str) -> RuntimeCheckpoint: ...
    def list_checkpoints(self, run_id: str) -> list[RuntimeCheckpoint]: ...
    def audit_expired_leases(self, now: datetime) -> list[RuntimeRun]: ...
```

Raise typed `RuntimeNotFound`, `RuntimeConflict`, `RuntimeLeaseLost`, `RuntimeIntegrityError`, and `RuntimePersistenceError` exceptions. Use one shared `RLock` for `InMemoryRuntimeStore` and `InMemoryInvestigationRepository` so a phase's business and Runtime mutations are atomic under the same lock. Use conditional SQL updates in SQLite.

- [ ] **Step 4: Add connection-aware business persistence**

Refactor existing SQLite writes into internal helpers that accept a caller-owned SQLAlchemy `Connection`. Public methods continue to open their own transaction. Add `save_with_connection`, `save_tasks_with_connection`, `save_tool_calls_with_connection`, and `save_multi_agent_result_with_connection` for Runtime atomic commits. The memory repository performs the equivalent mutation under the Runtime store lock.

- [ ] **Step 5: Verify store parity**

Run:

```powershell
uv run pytest tests/runtime/test_memory_store.py tests/runtime/test_sqlite_store.py tests/db/test_sqlite_repository.py -v
uv run ruff check backend/runtime backend/db/repositories.py backend/db/sqlite_repository.py tests/runtime
```

Expected: both stores pass the same behavioral suite and existing repository tests remain green.

### Task 5: Add the Lease-Fenced Single Writer and Atomic Phase Commit

**Files:**
- Create: `backend/runtime/writer.py`
- Create: `backend/runtime/phases.py`
- Modify: `backend/runtime/store.py`
- Modify: `backend/runtime/sqlite_store.py`
- Test: `tests/runtime/test_writer.py`

- [ ] **Step 1: Write atomicity and ordering tests**

Test concurrent event producers, injected persistence failure, stale lease results, and a successful phase commit. The critical assertion is:

```python
def test_phase_commit_is_all_or_nothing(sqlite_runtime_store, failpoint) -> None:
    run, attempt = running_attempt(sqlite_runtime_store)
    failpoint.raise_at("after_business_before_event")

    with pytest.raises(RuntimePersistenceError):
        sqlite_runtime_store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=run.lease_version,
                phase=RuntimePhase.EVIDENCE_COLLECTION,
                business_mutation=evidence_mutation("evidence-1"),
                safe_payload={"evidence_count": 1},
                resume_state=RuntimeResumeState(completed_evidence_ids=["evidence-1"]),
            )
        )

    assert investigation_evidence_ids(sqlite_runtime_store) == []
    assert sqlite_runtime_store.list_events(run.id) == prior_events
    assert sqlite_runtime_store.list_checkpoints(run.id) == []
```

- [ ] **Step 2: Run the tests and confirm atomic commit is absent**

Run:

```powershell
uv run pytest tests/runtime/test_writer.py -v
```

Expected: failure because `PhaseCommit` and the writer do not exist.

- [ ] **Step 3: Implement phase result and digest contracts**

Define `PhaseInput`, `PhaseOutput`, `BusinessMutation`, and `PhaseCommit`. `checkpoint_digest()` must serialize only schema version, Run/Attempt IDs, completed phase, referenced record IDs, remaining budgets, and successful tool keys using sorted JSON, then SHA-256 it.

Extend `RuntimeStore` after `PhaseCommit` exists:

```python
def commit_phase(self, commit: PhaseCommit) -> RuntimeCheckpoint: ...
```

- [ ] **Step 4: Implement atomic SQL commit**

Within one `engine.begin()` transaction:

1. Fence on `run_id`, `lease_owner`, `lease_version`, and unexpired lease.
2. Apply connection-aware business mutations.
3. Allocate sequences with `UPDATE runtime_runs SET next_event_sequence = next_event_sequence + 1 ... RETURNING next_event_sequence`.
4. Insert `phase.completed` and `checkpoint.created` events.
5. Insert the checkpoint with the last event sequence.
6. Update `current_phase` and `latest_checkpoint_id`.

Publish committed events to `RuntimeEventHub` only after the transaction succeeds. The memory implementation performs the same ordered mutation under one lock.

- [ ] **Step 5: Implement the central writer**

`RuntimeWriter` owns an `asyncio.Queue[WriterCommand]` and one consumer task. `submit()` returns an awaited Future. It rejects results after lease loss or shutdown and never performs network I/O.

- [ ] **Step 6: Verify writer behavior**

Run:

```powershell
uv run pytest tests/runtime/test_writer.py tests/runtime/test_sqlite_store.py -v
uv run ruff check backend/runtime/writer.py backend/runtime/phases.py backend/runtime/sqlite_store.py tests/runtime/test_writer.py
```

Expected: rollback, sequence, fencing, and successful commit tests pass.

### Milestone 1 Review Gate

Run:

```powershell
uv run pytest tests/domain/test_runtime_models.py tests/config/test_settings.py tests/db/test_runtime_migrations.py tests/runtime/test_memory_store.py tests/runtime/test_sqlite_store.py tests/runtime/test_writer.py -v
uv run ruff check backend/domain/runtime.py backend/runtime backend/db backend/config/settings.py
```

Review the diff against spec sections 7, 8, 11, 22, 23, and 24. Do not start Milestone 2 until store parity, atomic rollback, lease fencing, and secret rejection have no blocking findings.

## Milestone 2: Execution, Recovery, and Multi-session Concurrency

### Task 6: Extract Stable Diagnosis Phases Without Changing RCA Semantics

**Files:**
- Create: `backend/runtime/phase_executor.py`
- Modify: `backend/diagnosis/orchestrator.py`
- Test: `tests/runtime/test_phase_executor.py`
- Test: `tests/diagnosis/test_orchestrator.py`

- [ ] **Step 1: Freeze current orchestrator outputs in tests**

For every existing golden incident and both Fixed and Adaptive strategies, generate fixtures by executing the real V8.2 baseline commit `3f06d9ae87729185b3429c839f11cc366de75000` from an isolated archive. Store per-fixture SHA-256 checksums. Canonicalization maps random entity IDs while preserving cross-record references, removes only generated time/duration fields, normalizes semantically unordered ID sets, and hashes report Markdown by semantic section. Compare the durable executor against these committed fixtures; do not simulate the baseline with current shared code.

- [ ] **Step 2: Add failing phase boundary tests**

Assert this exact phase order and `conflict_review` skip behavior:

```python
assert executed_phases == [
    RuntimePhase.INTAKE,
    RuntimePhase.EVIDENCE_COLLECTION,
    RuntimePhase.DETERMINISTIC_RCA,
    RuntimePhase.SPECIALIST_ANALYSIS,
    RuntimePhase.CONFLICT_REVIEW,
    RuntimePhase.COORDINATION,
    RuntimePhase.REPORT_GENERATION,
    RuntimePhase.FINALIZE,
]
assert outputs[RuntimePhase.CONFLICT_REVIEW].status in {"completed", "skipped"}
```

- [ ] **Step 3: Implement `DiagnosisPhaseExecutor`**

Expose one async method per stable phase and return immutable `PhaseOutput`. Execute synchronous Provider and deterministic code with `asyncio.to_thread`; await the Agents runtime directly. Use the Run's frozen strategy, Provider, model, prompt version, and budgets. Do not read process-global current-session state.

Phase mapping:

```text
intake                 -> create or load InvestigationRecord and sanitize IncidentEvent
evidence_collection    -> coordinator.collect + evidence validation
deterministic_rca       -> analyzer.analyze + semantic validation
specialist_analysis    -> existing V5 findings and optional V8.2 Agents runtime
conflict_review        -> affected-specialist review or explicit skipped output
coordination           -> validated candidates, review, run summary, actions, verifications
report_generation      -> report generator
finalize               -> status completion and final integrity validation
```

- [ ] **Step 4: Retain synchronous compatibility**

Make `DiagnosisOrchestrator.run()` a compatibility façade that runs the same phase executor through a local non-durable adapter. Existing direct unit tests and callers continue to receive `InvestigationRecord`; no Runtime table is required for an explicitly disabled Runtime.

- [ ] **Step 5: Verify no RCA regression**

Run:

```powershell
uv run pytest tests/runtime/test_phase_executor.py tests/diagnosis/test_orchestrator.py tests/golden -v
```

Expected: phase tests pass and normalized pre/post RCA outputs are identical.

### Task 7: Add Cooperative Model and Tool Boundaries with Idempotence

**Files:**
- Modify: `backend/domain/tool_calls.py`
- Modify: `backend/diagnosis/adaptive_tools.py`
- Modify: `backend/diagnosis/agents_runtime.py`
- Test: `tests/runtime/test_tool_idempotence.py`
- Test: `tests/diagnosis/test_adaptive_tools.py`
- Test: `tests/diagnosis/test_agents_runtime.py`

- [ ] **Step 1: Write failing idempotence tests**

Test successful Tool reuse after crash, interrupted Tool state, duplicate logical results, new LLM execution ID after ambiguous crash, cancellation before and after external boundaries, and late results after lease loss.

```python
async def test_resume_reuses_committed_tool_result(fake_registry, tool_journal) -> None:
    tool_journal.record_success("key-1", call=successful_call("tool-1"))
    session = adaptive_session(
        registry=fake_registry,
        resolve_tool_result=tool_journal.resolve,
        persist_tool_result=tool_journal.persist,
    )

    response = await session.invoke(AgentName.LOG, "read_logs", SAFE_QUERY, 1)

    assert response_status(response) == "success"
    assert fake_registry.invocation_count == 0
```

- [ ] **Step 2: Run focused tests and confirm hooks are missing**

Run:

```powershell
uv run pytest tests/runtime/test_tool_idempotence.py tests/diagnosis/test_adaptive_tools.py tests/diagnosis/test_agents_runtime.py -v
```

Expected: new idempotence tests fail while existing tests remain as the compatibility baseline.

- [ ] **Step 3: Extend ToolCallRecord additively**

Add optional `runtime_run_id`, `logical_call_id`, `idempotency_key`, and `execution_id` fields with `None` defaults. Existing persisted payloads remain readable. Compute the idempotency key from Run ID, Agent, logical step, Tool name, and normalized safe input; never include secrets or raw evidence.

- [ ] **Step 4: Add resolver and sink interfaces**

`AdaptiveToolSession` accepts:

```python
resolve_tool_result: Callable[[str], ToolCallRecord | None]
persist_tool_result: Callable[[ToolExecutionResult], Awaitable[ToolCallRecord]]
check_execution: Callable[[], None]
```

Call `check_execution()` before and after Provider, Tool, and model boundaries. Resolve committed successes before invocation. Preallocate IDs before Tool execution. Persist success through the Runtime writer before returning it to an Agent. Mark ambiguous in-progress calls interrupted during recovery; never synthesize success.

- [ ] **Step 5: Give every model request an execution ID**

Pass `execution_id` through Agents runtime progress and events. Recovery may invoke the logical step again using a new execution ID; the writer accepts only the first result that matches the active lease and unfinished logical step.

- [ ] **Step 6: Verify idempotence and existing Adaptive behavior**

Run:

```powershell
uv run pytest tests/runtime/test_tool_idempotence.py tests/diagnosis/test_adaptive_tools.py tests/diagnosis/test_agents_runtime.py -v
uv run ruff check backend/domain/tool_calls.py backend/diagnosis/adaptive_tools.py backend/diagnosis/agents_runtime.py tests/runtime/test_tool_idempotence.py
```

Expected: committed Tools are reused, late results are rejected, and V8.2 tool budgets and safety tests still pass.

### Task 8: Implement Runtime Coordinator, Cancellation, and Manual Recovery

**Files:**
- Create: `backend/runtime/coordinator.py`
- Create: `backend/runtime/faults.py`
- Modify: `backend/runtime/phase_executor.py`
- Test: `tests/runtime/test_coordinator.py`
- Test: `tests/runtime/test_fault_injection.py`

- [ ] **Step 1: Write lifecycle and recovery tests**

Cover every legal/illegal transition, checkpoint-after-phase behavior, explicit cancellation, expired lease audit, new Attempt on resume, digest/reference validation, bad checkpoint rejection, model ambiguity, and no automatic resume on startup.

- [ ] **Step 2: Run tests and confirm coordinator is missing**

Run:

```powershell
uv run pytest tests/runtime/test_coordinator.py tests/runtime/test_fault_injection.py -v
```

Expected: import failures for coordinator and fault injection registry.

- [ ] **Step 3: Implement the coordinator loop**

`RuntimeCoordinator.execute(run_id, owner)` must:

1. Acquire or validate the Run lease.
2. Create a new Attempt and `attempt.started` event.
3. Start lease renewal every `heartbeat_seconds`.
4. Iterate only phases after the validated checkpoint.
5. Check cancellation and ownership before/after each external boundary.
6. Commit each successful phase through `RuntimeWriter`.
7. Finish Run and Attempt atomically or persist a safe failure/interruption category.

Use `asyncio.TaskGroup` only for bounded child work. On lease loss, stop accepting child results and transition through the audit path rather than writing with a stale fence.

- [ ] **Step 4: Implement explicit cancel and resume**

`request_cancel()` writes `cancel_requested_at`, transitions to `cancelling`, and signals the local task if present. `resume()` accepts only `interrupted`, validates schema/digest/references/budgets, conditionally acquires a lease, creates a new Attempt linked to the checkpoint, and starts after `completed_phase`. Cancelled, failed, and completed Runs return conflict.

- [ ] **Step 5: Add production-disabled fault points**

Implement the 11 Milestone 2 hooks that have real execution/recovery seams: phase/provider/tool/model/persistence boundaries, lease loss, concurrent resume, checkpoint tamper, parallel-session failure, parallel cancellation, and unsafe event payload. Construction requires an injected `FaultInjector`; production uses `NoFaultInjector`. Tests use `DeterministicFaultInjector`. No environment variable enables faults in production.

Defer `sse_reconnect`, `otel_unavailable`, and `replay_external_call` until their Milestone 3 implementations exist. Keep them in an explicit deferred registry, but do not wire or claim them in Milestone 2 tests.

- [ ] **Step 6: Verify lifecycle and recovery**

Run:

```powershell
uv run pytest tests/runtime/test_coordinator.py tests/runtime/test_fault_injection.py -v
uv run ruff check backend/runtime/coordinator.py backend/runtime/faults.py tests/runtime/test_coordinator.py tests/runtime/test_fault_injection.py
```

Expected: interruption stops at the last full checkpoint, one resume wins, invalid checkpoint remains interrupted, and cancelled Runs cannot resume.

### Task 9: Add Bounded Multi-session Scheduling

**Files:**
- Create: `backend/runtime/manager.py`
- Modify: `backend/runtime/phase_executor.py`
- Test: `tests/runtime/test_manager.py`

Multiple Investigations may execute concurrently, while each Investigation retains the one-active-live-Run invariant enforced by the store.

- [ ] **Step 1: Write concurrency and isolation tests**

Create four Investigations with separate fake Providers, budgets, models, and event collectors. Block them at a barrier and assert all four are active. Make one fail and assert the other three finish. Assert a fifth waits, a second live Run in one Investigation conflicts, and per-Run parallel steps never exceed three.

- [ ] **Step 2: Run tests and confirm manager is missing**

Run:

```powershell
uv run pytest tests/runtime/test_manager.py -v
```

Expected: import failure for `RuntimeManager`.

- [ ] **Step 3: Implement process-wide scheduling**

`RuntimeManager` owns:

```python
self._run_limit = asyncio.Semaphore(settings.max_concurrent_runs)
self._tasks: dict[str, asyncio.Task[None]] = {}
self._coordinators: dict[str, RuntimeCoordinator] = {}
self._lock = asyncio.Lock()
```

`start(run_id)` is idempotent for the same local Run and creates one background task. `shutdown()` stops new starts, requests cooperative interruption, waits within a bounded shutdown budget, and leaves uncompleted Runs recoverable through lease expiry.

- [ ] **Step 4: Bound parallel phase work**

Use a per-Run semaphore with `max_parallel_steps_per_run`. Evidence Providers, Log/Metric/Deployment specialists, and affected conflict reviewers can run concurrently. Deterministic RCA, coordination, report, and finalize remain ordered. One specialist's structured failure does not cancel siblings; global timeout, cancellation, or lease loss cancels the task group.

- [ ] **Step 5: Verify real parallelism and isolation**

Run:

```powershell
uv run pytest tests/runtime/test_manager.py tests/db/test_concurrency.py -v
uv run ruff check backend/runtime/manager.py backend/runtime/phase_executor.py tests/runtime/test_manager.py
```

Expected: four Runs overlap, per-Run work is bounded at three, and cross-session state remains empty.

### Milestone 2 Review Gate

Run:

```powershell
uv run pytest tests/runtime/test_phase_executor.py tests/runtime/test_tool_idempotence.py tests/runtime/test_coordinator.py tests/runtime/test_manager.py tests/runtime/test_fault_injection.py tests/diagnosis tests/golden -v
uv run ruff check backend/runtime backend/diagnosis backend/domain/tool_calls.py
```

Review against spec sections 5, 8-14, and 25. Explicitly inspect network-call transaction boundaries, shared mutable state, lease fences, cancellation checks, side-effect reuse, and Fixed/Adaptive equivalence.

## Milestone 3: APIs, Events, Observability, Replay, Diff, and UI

### Task 10: Add Runtime APIs and Backward-compatible Investigation Entry Points

**Files:**
- Create: `backend/api/runtime_runs.py`
- Modify: `backend/api/investigations.py`
- Modify: `backend/api/events.py`
- Modify: `backend/db/models.py`
- Modify: `backend/services/container.py`
- Modify: `backend/main.py`
- Test: `tests/api/test_runtime_runs_api.py`
- Test: `tests/api/test_investigations_api.py`
- Test: `tests/api/test_events_api.py`

- [ ] **Step 1: Write API contract tests**

Assert `202` creation, Run history, detail, event pagination, cancel/resume conflicts, replay/diff routing, `runtime_available=false` for old records, and `runtime_available=true` for new records. Assert every endpoint rejects mismatched Investigation/Run relationships.

- [ ] **Step 2: Run tests and confirm routes are absent**

Run:

```powershell
uv run pytest tests/api/test_runtime_runs_api.py tests/api/test_investigations_api.py tests/api/test_events_api.py -v
```

Expected: new Runtime routes return `404` or fail import.

- [ ] **Step 3: Construct Runtime services in the container**

Build the memory or SQLite Runtime store beside the existing Investigation repository, sharing the SQLite engine where applicable. Construct EventHub, Writer, Telemetry, PhaseExecutor, Coordinator factory, and Manager. `AppContainer.close()` shuts them down through the FastAPI lifespan.

- [ ] **Step 4: Use FastAPI lifespan**

Add an `@asynccontextmanager` lifespan that starts the writer, runs the expired-lease audit without auto-resume, starts the manager, yields, then performs bounded shutdown and OTel flush. Tests must use `with TestClient(app)` when lifecycle behavior matters.

- [ ] **Step 5: Implement additive routes**

Implement all approved endpoints. The create request accepts strategy, run reason, optional parent Run, and frozen model/prompt metadata. Return `202` with `RuntimeRun`. Map not found to `404`, lifecycle/active-Run conflicts to `409`, invalid checkpoint to `422`, and unavailable Runtime to `503`.

- [ ] **Step 6: Preserve old synchronous APIs**

Existing `/investigations/manual` and simulated event endpoints create a Runtime Run and await its terminal state before returning the same response model. When `runtime.enabled=false`, they use the compatibility orchestrator. Add `runtime_available: bool = False` to `InvestigationRecord` and `InvestigationSummary` for old-record compatibility, but do not persist it as business state; API projection sets it to `True` only when the Runtime store has at least one Run for that Investigation.

- [ ] **Step 7: Verify API compatibility**

Run:

```powershell
uv run pytest tests/api/test_runtime_runs_api.py tests/api/test_investigations_api.py tests/api/test_events_api.py -v
uv run ruff check backend/api backend/services/container.py backend/main.py backend/db/models.py
```

Expected: new contracts pass and pre-V9 API shapes remain compatible.

### Task 11: Implement Durable Catch-up Plus Live SSE

**Files:**
- Create: `backend/runtime/event_hub.py`
- Modify: `backend/api/runtime_runs.py`
- Test: `tests/runtime/test_event_hub.py`
- Test: `tests/api/test_runtime_sse.py`

- [ ] **Step 1: Write SSE race and reconnect tests**

Test initial catch-up, event arriving between subscribe and query, reconnect from an old `Last-Event-ID`, heartbeat without ID, two independent Run streams, slow consumer overflow, and disconnect without cancellation.

- [ ] **Step 2: Run tests and confirm stream behavior is absent**

Run:

```powershell
uv run pytest tests/runtime/test_event_hub.py tests/api/test_runtime_sse.py -v
```

Expected: failures because EventHub and stream route are missing.

- [ ] **Step 3: Implement gap-free delivery**

Register a bounded subscriber queue before database catch-up, query events after the requested sequence, deliver committed rows in sequence order, then consume queued notifications while discarding sequences already delivered. On queue overflow, close that subscriber so the client reconnects from its last durable sequence; never block the writer.

- [ ] **Step 4: Format SSE frames**

Business frames use:

```text
id: <sequence>
event: <event_type>
data: <safe RuntimeEvent JSON>
```

Heartbeat frames are comments (`: heartbeat`) with no ID. Set `Cache-Control: no-cache`, `Connection: keep-alive`, and `X-Accel-Buffering: no`. Stop generation when `request.is_disconnected()` is true.

- [ ] **Step 5: Verify no gaps or duplicate business events**

Run:

```powershell
uv run pytest tests/runtime/test_event_hub.py tests/api/test_runtime_sse.py -v
uv run ruff check backend/runtime/event_hub.py backend/api/runtime_runs.py tests/runtime/test_event_hub.py tests/api/test_runtime_sse.py
```

Expected: sequence projections are exactly-once per client after deduplication, while heartbeats remain unsequenced.

### Task 12: Add Privacy-safe OpenTelemetry

**Files:**
- Create: `backend/runtime/telemetry.py`
- Modify: `backend/runtime/coordinator.py`
- Modify: `backend/runtime/phase_executor.py`
- Test: `tests/runtime/test_telemetry.py`

- [ ] **Step 1: Write in-memory span tests**

Use `InMemorySpanExporter` to assert one trace per Attempt, Phase/Agent/Model/Tool parentage, a Span Link from resumed Attempt to prior Attempt, exact allowed attributes, disabled-mode no-op behavior, and collector failure isolation.

- [ ] **Step 2: Run tests and confirm telemetry is absent**

Run:

```powershell
uv run pytest tests/runtime/test_telemetry.py -v
```

Expected: import failure for Runtime telemetry.

- [ ] **Step 3: Implement allowlisted tracing**

Create a dedicated `TracerProvider(Resource.create({SERVICE_NAME: "diagops-runtime"}))`. Use `BatchSpanProcessor` and OTLP/HTTP `OTLPSpanExporter` only when enabled with a validated endpoint. Project only the approved low-cardinality attributes. Apply redaction before `set_attribute`; reject prompt, evidence body, free-text output, credentials, and provider payload fields.

- [ ] **Step 4: Link resumed Attempts**

Persist the current Attempt's fixed-length trace and root-span IDs only in the internal `runtime_attempts` columns added by Task 3. Read the previous Attempt's internal identifiers to start a new root span with an OTel `Link`; do not reuse the old trace ID and do not expose these internal identifiers through events or public APIs.

- [ ] **Step 5: Make export failure non-fatal**

OTel creation failure falls back to a no-op tracer with a safe warning category. Export happens through the batch processor outside Runtime database transactions. Shutdown uses a bounded flush and never changes Run status.

- [ ] **Step 6: Verify telemetry safety**

Run:

```powershell
uv run pytest tests/runtime/test_telemetry.py tests/safety/test_redaction.py -v
uv run ruff check backend/runtime/telemetry.py backend/runtime/coordinator.py backend/runtime/phase_executor.py tests/runtime/test_telemetry.py
```

Expected: hierarchy and links are complete, prohibited attributes are absent, and exporter failure does not fail a Run.

### Task 13: Implement Zero-external-call Replay and Structured Run Diff

**Files:**
- Create: `backend/runtime/replay.py`
- Create: `backend/runtime/diff.py`
- Modify: `backend/api/runtime_runs.py`
- Modify: `backend/benchmarks/openrca/runner.py`
- Test: `tests/runtime/test_replay.py`
- Test: `tests/runtime/test_diff.py`
- Test: `tests/benchmarks/test_openrca_runner.py`

- [ ] **Step 1: Write replay safety and corruption tests**

Inject Provider, Tool, and model sentinels that raise on every call. Replay a valid Run and assert all counts remain zero. Then test event gaps, duplicate sequence, illegal transitions, bad references, altered checkpoint digest, unsupported schema, and an attempted external call.

- [ ] **Step 2: Write stable Diff fixtures**

Compare two fixed fixtures twice and assert byte-equivalent JSON after canonical serialization. Cover strategy/model/prompt, phase duration/status, Agent outcomes, normalized Tool inputs, Evidence references, causes/confidence, tokens/cost/duration, fallback, and failure categories. Assert prompt and model prose are absent.

- [ ] **Step 3: Run tests and confirm services are absent**

Run:

```powershell
uv run pytest tests/runtime/test_replay.py tests/runtime/test_diff.py tests/benchmarks/test_openrca_runner.py -v
```

Expected: Replay and Diff imports fail; existing benchmark tests establish the baseline.

- [ ] **Step 4: Implement audit replay**

Replay creates a terminal `run_kind=replay` Run linked by `source_run_id`. It reads only Runtime and persisted business repositories, validates ordered events, legal transitions, schema versions, checkpoint digest, and record references, then reruns deterministic semantic validators. The replay dependency container exposes no Provider, Tool registry, or model.

For Runs created by the OpenRCA runner, persist the Runtime Run ID in the frozen prediction row metadata and rerun the repository's compatible evaluator over persisted prediction output. For normal Investigations, record benchmark evaluation as `not_applicable`, not as success or failure.

- [ ] **Step 5: Implement structured Diff**

Normalize timestamps to durations, sort IDs and Tool inputs, compare enums and metrics, and return typed sections with `left`, `right`, and `changed`. Hash approved free-text business fields only when change detection is needed; never return their content through Diff.

- [ ] **Step 6: Wire routes and verify**

Run:

```powershell
uv run pytest tests/runtime/test_replay.py tests/runtime/test_diff.py tests/benchmarks/test_openrca_runner.py tests/api/test_runtime_runs_api.py -v
uv run ruff check backend/runtime/replay.py backend/runtime/diff.py backend/benchmarks/openrca/runner.py backend/api/runtime_runs.py
```

Expected: replay makes zero external calls, detects every corruption fixture, and Diff output is stable.

### Task 14: Build the Multi-session Runtime Workbench

**Files:**
- Create: `frontend/src/RuntimeWorkbench.tsx`
- Create: `frontend/src/useRuntimeEvents.ts`
- Create: `frontend/src/runtimeProjection.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `tests/frontend/test_runtime_workbench.py`

- [ ] **Step 1: Add failing frontend source-contract tests**

Assert the frontend declares all Runtime API types, uses `EventSource`, stores last sequence per Run, renders session status, phase swimlanes, historical Run selection, event detail, and valid cancel/resume/replay/diff controls without unsafe raw payload fields.

- [ ] **Step 2: Run frontend tests and confirm components are absent**

Run:

```powershell
uv run pytest tests/frontend/test_runtime_workbench.py tests/frontend/test_frontend_smoke.py -v
```

Expected: failures because Runtime workbench files do not exist.

- [ ] **Step 3: Add typed API functions**

Add `RuntimeRun`, `RuntimeAttempt`, `RuntimeEvent`, `RuntimeCheckpoint`, `ReplayReport`, and `RuntimeRunDiff` types plus functions for every approved endpoint. Keep `safe_payload` typed as `Record<string, JsonValue>` and do not introduce raw prompt/provider fields.

- [ ] **Step 4: Implement reconnectable event state**

`useRuntimeEvents(runId)` keeps `{lastSequence, events}` keyed by Run ID. On reconnect, pass the last sequence using a query fallback accepted by the API because browser `EventSource` cannot set custom headers; the API still honors standard `Last-Event-ID` for non-browser clients. Deduplicate by sequence and refetch durable events after an error before reopening the stream with bounded backoff.

- [ ] **Step 5: Implement the three-panel workbench**

The left panel lists multiple Investigation sessions and current Run status. The middle panel shows phase timeline and parallel Agent swimlanes. The right panel shows allowlisted Event, Tool, Evidence reference, and Checkpoint detail. Run history is read-only; valid controls call cancel, resume, replay, or diff and invalidate TanStack Query keys.

- [ ] **Step 6: Verify responsive UI and build**

Run:

```powershell
uv run pytest tests/frontend/test_runtime_workbench.py tests/frontend/test_frontend_smoke.py -v
npm.cmd --prefix frontend run build
```

Expected: frontend source-contract tests pass and Vite builds without TypeScript errors.

### Milestone 3 Review Gate

Run:

```powershell
uv run pytest tests/api/test_runtime_runs_api.py tests/api/test_runtime_sse.py tests/runtime/test_event_hub.py tests/runtime/test_telemetry.py tests/runtime/test_replay.py tests/runtime/test_diff.py tests/frontend/test_runtime_workbench.py -v
npm.cmd --prefix frontend run build
```

Review against spec sections 15-21 and 24. Inspect SSE race handling, OTel attributes, replay dependency construction, Diff normalization, browser reconnection, and UI separation between multiple sessions and intra-Run parallelism.

## Milestone 4: Acceptance, Regression, and Operations

### Task 15: Add the Runtime Acceptance and Fault-injection Gate

**Files:**
- Create: `backend/services/runtime_acceptance.py`
- Test: `tests/services/test_runtime_acceptance.py`
- Expand: `tests/runtime/test_fault_injection.py`

- [ ] **Step 1: Write key-free acceptance runner tests**

Use deterministic fake Providers and model adapters. Assert the runner writes one JSON artifact containing configuration, Git commit, scenario results, latency, database growth, OTel overhead, recovery time, and privacy scan result. The runner fails if any required scenario is missing or any secret marker appears.

- [ ] **Step 2: Run tests and confirm the runner is absent**

Run:

```powershell
uv run pytest tests/services/test_runtime_acceptance.py tests/runtime/test_fault_injection.py -v
```

Expected: import failure for the acceptance runner.

- [ ] **Step 3: Implement all acceptance scenarios**

Run the 14 approved fault injections plus:

- four concurrent sessions with one isolated failure;
- max three parallel steps per Run;
- SSE reconnect with no missing or duplicate business events;
- concurrent resume with exactly one winner;
- successful Tool reuse;
- replay with zero external calls;
- OTel unavailable and default-off modes;
- cross-artifact credential and raw-payload scan.

Use a fixed seed and bounded per-scenario timeout. Artifacts contain only allowlisted structured data under `output/runtime-acceptance/<run-id>/result.json` and are not committed.

- [ ] **Step 4: Add performance measurements without hiding regressions**

Measure V8.2-compatible synchronous execution versus Runtime-disabled and Runtime-enabled execution over the same deterministic cases. Report p50/p95 wall time, SQLite bytes per Run, event count, checkpoint count, recovery duration, and OTel enabled/disabled overhead. Do not introduce a pass threshold not approved by the spec; preserve raw measurements and comparison percentages.

- [ ] **Step 5: Run the key-free gate**

Run:

```powershell
uv run python -m backend.services.runtime_acceptance
uv run pytest tests/services/test_runtime_acceptance.py tests/runtime/test_fault_injection.py -v
```

Expected: the command exits `0`, writes a safe artifact, and every required scenario reports `passed`.

### Task 16: Update Operations Documentation and Migration Guidance

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/current.md`
- Verify: `config/diagops.yaml`

- [ ] **Step 1: Document Runtime operations**

Add configuration and environment overrides, multi-session behavior, one-active-live-Run rule, Run and Attempt lifecycle, SSE reconnect, cancel versus resume semantics, startup recovery audit, OTel default-off setup, audit replay, Run Diff, schema V6 migration, and event retention policy.

- [ ] **Step 2: Document safety boundaries**

State that Runtime events, SSE, OTel, Replay, and Diff exclude prompts, chain-of-thought, credentials, evidence bodies, arbitrary provider payloads, and production mutations. State that collector failure and client disconnect do not affect execution.

- [ ] **Step 3: Update implementation status only from evidence**

During development, set `Iteration status: implementing` and `Implementation status: in_progress`. After all key-free gates pass, set both to `verifying`. Do not mark `complete` until the real V8.2/V9 OpenRCA comparison and all final checks are recorded with artifact paths and a completion commit supplied by an explicitly authorized Git action.

- [ ] **Step 4: Check documentation references**

Run:

```powershell
rg -n "V9|runtime-runs|Last-Event-ID|interrupted|OpenTelemetry|replay|diff|schema V6|read-only" README.md docs/superpowers/current.md
```

Expected: each operational concept is discoverable and no section claims automatic recovery or production mutation.

### Task 17: Run Full Regression and the OpenRCA Release Gate

**Files:**
- Verify only unless failures reveal implementation defects.
- Record evidence in: `docs/superpowers/current.md`

- [ ] **Step 1: Run all key-free static, backend, and frontend checks**

Run:

```powershell
uv run ruff check .
uv run pytest -v
npm.cmd --prefix frontend run build
uv run python -m backend.services.runtime_acceptance
```

Expected: every command exits `0`; pytest reports no failed tests; Vite builds successfully; Runtime acceptance reports every required scenario passed.

- [ ] **Step 2: Run database integrity checks on a migrated copy**

Run the documented migration against a copy of a V8.2 SQLite database, then execute:

```powershell
uv run python -c "from backend.config.settings import load_settings; from backend.db.session import create_db_engine, initialize_database; e=create_db_engine(load_settings().storage.url); initialize_database(e); print('foreign_key_check=passed')"
```

Expected: output is `foreign_key_check=passed`; existing Investigations remain readable with `runtime_available=false`.

- [ ] **Step 3: Run the real paired OpenRCA 40-case workflow**

Use the exact prepare, run, evaluate, and upstream-evaluator commands documented in README with the same frozen dataset selection, model, prompt version, and caller-supplied prices as the V8.2 baseline. Do not expose credentials in commands, logs, chat, or artifacts.

Expected:

```text
40 Fixed predictions
40 Adaptive predictions
Adaptive completion >= 95%
Evidence reference validity = 100%
Read-only violations = 0
Upstream official Adaptive partial score >= frozen V8.2 baseline
```

- [ ] **Step 4: Verify Replay and Diff against real frozen Runs**

Replay at least one Fixed and one Adaptive Runtime Run from the 40-case workflow. Assert zero external calls and attach their `ReplayReport` identifiers. Generate a Diff for the paired Runs and verify stable hashes on two reads.

- [ ] **Step 5: Record release evidence and residual risk**

Record exact command results, pytest count, build result, Runtime acceptance artifact path, OpenRCA run ID, official report path, V8.2 baseline comparison, performance report, skipped checks, and unresolved risks in `docs/superpowers/current.md`. If credentials or the full dataset are unavailable, keep V9 at `verifying` and state that the live release gate is blocked; do not claim completion.

## Final Review Checklist

Before requesting implementation completion approval, verify every item:

- [ ] One Investigation is one session; multiple sessions can execute concurrently.
- [ ] One session has at most one active live Run and may retain multiple historical Runs.
- [ ] Run, Attempt, Event, and Checkpoint contracts match the approved spec.
- [ ] Event sequences are continuous, unique, and allocated by the durable writer.
- [ ] Phase business mutations, completion events, and checkpoints commit atomically.
- [ ] All lease-sensitive writes fence on owner and version.
- [ ] Expired Runs become interrupted and never auto-resume.
- [ ] Cancelled Runs are terminal; only interrupted Runs resume.
- [ ] Resume creates a new Attempt and does not repeat committed Tool successes.
- [ ] External I/O never occurs inside a SQLite transaction.
- [ ] Four Runs and three per-Run steps are bounded independently.
- [ ] SSE catch-up/live bridging has no event gap and does not control execution.
- [ ] OTel is default-off, allowlisted, linked across Attempts, and non-fatal.
- [ ] Replay has no Provider, Tool, model, or production-data dependency.
- [ ] Diff compares stable structured data and exposes no hidden reasoning.
- [ ] Old Investigations remain readable without fabricated Runtime history.
- [ ] Fixed, Adaptive, fallback, Evidence, Review, Report, and API semantics do not regress.
- [ ] The 14 fault scenarios and cross-artifact privacy scan pass.
- [ ] Full Ruff, pytest, frontend build, Runtime acceptance, migration integrity, and OpenRCA gates have fresh evidence.

## Spec Coverage Matrix

| Approved spec sections | Implementation tasks |
| --- | --- |
| 1-6 Summary, positioning, goals, boundaries, sessions, architecture | Tasks 1, 6, 9, 10 |
| 7 Runtime data contracts | Tasks 2-5 |
| 8 Lifecycle and state machine | Tasks 2, 4, 8 |
| 9 Stable phases | Tasks 5, 6 |
| 10 Concurrency | Tasks 6, 9, 15 |
| 11 Lease and ownership | Tasks 4, 5, 8 |
| 12 Tool and model idempotence | Task 7 |
| 13-14 Cancellation and recovery | Task 8 |
| 15 Runtime events | Tasks 2, 5, 8 |
| 16 SSE | Task 11 |
| 17 OpenTelemetry | Tasks 2, 12 |
| 18 Audit replay | Task 13 |
| 19 Run Diff | Task 13 |
| 20 API surface | Task 10 |
| 21 Runtime UI | Task 14 |
| 22 Persistence and migration | Tasks 3-5 |
| 23 Configuration | Task 2 |
| 24 Security and privacy | Tasks 2, 7, 11-13, 15 |
| 25 Fault injection | Tasks 8, 15 |
| 26-27 Acceptance and verification | Tasks 15-17 |
| 28 Implementation sequence | Milestones 1-4 |
| 29 Decisions and trade-offs | Execution Preconditions and Deliberate Simplifications |
| 30 Approval gate | Plan Approval Gate |

## Deliberate Simplifications

1. Execution is in-process. SQLite leases provide ownership and crash detection, not distributed scheduling.
2. Runtime events are an audit log, not the source for rebuilding all business state.
3. Recovery occurs only at completed phase checkpoints.
4. LLM invocation is at-least-once across ambiguous crashes; logical result acceptance is fenced and idempotent.
5. The first V9 release has no automatic event retention or cleanup.
6. Normal Investigation replay reports benchmark evaluation as `not_applicable`; only Runs linked to OpenRCA cases invoke the deterministic benchmark evaluator.

## Implementation References

- FastAPI lifecycle uses the recommended `lifespan` async context manager: <https://fastapi.tiangolo.com/advanced/events/>.
- OTel uses the official OTLP/HTTP exporter, `TracerProvider`, and `BatchSpanProcessor` pattern: <https://opentelemetry.io/docs/languages/python/exporters/>.
- OTLP exporter failure and timeout remain outside Runtime correctness: <https://opentelemetry.io/docs/specs/otel/protocol/exporter/>.

## Approval Gate

This plan is approved for execution. Each Milestone is implemented by one implementation subagent and then reviewed by one independent review subagent for specification compliance, code quality, and test coverage. Blocking findings return to the original implementation subagent and are re-reviewed by the same reviewer until the gate passes. The main thread only coordinates and summarizes these steps. After all four Milestones pass their gates, an overall review and final verification are required.
