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

- [x] **Step 1: Run all key-free static, backend, and frontend checks**

Run:

```powershell
uv run ruff check .
uv run pytest -v
npm.cmd --prefix frontend run build
uv run python -m backend.services.runtime_acceptance
```

Expected: every command exits `0`; pytest reports no failed tests; Vite builds successfully; Runtime acceptance reports every required scenario passed.

Status: verified on 2026-07-22. Exact results and the Runtime acceptance
artifact path are recorded in `docs/superpowers/current.md`.

- [x] **Step 2: Run database integrity checks on a migrated copy**

Run the documented migration against a copy of a V8.2 SQLite database, then execute:

```powershell
uv run python -c "from backend.config.settings import load_settings; from backend.db.session import create_db_engine, initialize_database; e=create_db_engine(load_settings().storage.url); initialize_database(e); print('foreign_key_check=passed')"
```

Expected: output is `foreign_key_check=passed`; existing Investigations remain readable with `runtime_available=false`.

Status: verified on 2026-07-22 against a copied V8.2-compatible schema V5
database. Evidence is recorded in `docs/superpowers/current.md`.

- [ ] **Step 3: Run the real paired OpenRCA 40-case workflow**

Use the exact prepare, run, evaluate, and upstream-evaluator commands documented in README with the same frozen dataset selection, model, prompt version, and caller-supplied prices as the V8.2 baseline. Do not expose credentials in commands, logs, chat, or artifacts.

Status: batch 01/05 run `run-20260726T065516824118Z` from correction commit
`087b404` is preserved for audit. All 16 Runtime Runs reached `completed`,
Model lifecycles were closed, no Phase failed, and benchmark token totals
exactly matched completed Runtime Model events. Fixed `Bank:51` nevertheless
produced an empty prediction: its first three specialist Model calls completed,
then six consecutive calls failed during coordination and conflict review.
All six raw connection errors in the batch were concentrated in this case;
Adaptive `Bank:51` and the other 14 Runs produced non-empty predictions.

Run only the paired `Bank:51` case from the frozen index in a new artifact root
and Runtime database. A valid recovery must keep both Fixed and Adaptive
results from the same retry, preserve the failed batch artifact, and pass the
same completion, non-empty prediction, evidence-reference, Model lifecycle,
Phase, and token-equality gates. After it passes, construct an audited merged
batch01 artifact that replaces both original `Bank:51` rows and records both
source Run IDs. Do not silently overwrite or cherry-pick a single strategy.

Recovery result: `run-20260726T090913094139Z` under
`D:\data\OpenRCA\results-v9-087b404-batch01-recovery-bank51` passed an
independent read-only audit. Both predictions are non-empty; both Runtime Runs
completed without Model or Phase failure; Model lifecycles are closed at
`8=8+0` and `13=13+0`; benchmark and Runtime tokens match exactly at Fixed
`9227/4043` and Adaptive `16004/5816`. The PowerShell wrapper stopped after the
run because `PSObject.Properties` does not expose a direct `Count` property;
this wrapper-only defect did not affect the saved benchmark or Runtime data.

Batch01 merge result: `run-merged-20260726T092848877633Z` under
`D:\data\OpenRCA\results-v9-087b404-batch01-merged` replaces both Fixed and
Adaptive Bank:51 rows from the paired recovery and records the source Run IDs,
Runtime databases, row-level Runtime sources, and source checksums. Independent
audit found 8 non-empty rows per strategy, 16 unique completed Runtime Run IDs,
closed Model lifecycles, no Phase failure, and exact Runtime token totals:
Fixed `87732/27142`, Adaptive `126352/38108`. Manifest SHA-256 is
`27f90b370fc97dbddda3279cdc17f768406ba0a54e8bfde320ac11a14bbb6c05`.
The manifest explicitly retains the original batch average duration because
the original per-case `perf_counter` duration was not persisted; the recovery
summary remains the authoritative exact duration for the replaced case.

Batch02 original result: `run-20260726T093723322944Z` under
`D:\data\OpenRCA\results-v9-087b404-batch02` is preserved for audit. Fixed
completed 8/8 with non-empty predictions. Adaptive completed 7/8; only
Telecom:5 failed. Its three specialist phases and every Model lifecycle
completed, but `AgentsRcaRuntime.synthesize()` raised `EvidenceContractError`
while constructing the final semantic review. The split Runtime path allowed
that fail-closed validation error to fail the Coordination Phase, while the
existing one-shot orchestration path maps the same boundary to
`semantic_reference` and deterministic fallback. No connection, tracing,
executor, lifecycle, or token-accounting defect contributed to this failure.

- [x] **Step 3a: Add the split-Runtime semantic fallback regression test**

Modify `tests/runtime/test_phase_executor.py`. Drive the real split
`AgentsRcaRuntime` through all Runtime phases, force final review construction
to raise `EvidenceContractError`, and assert the Runtime and Investigation
complete with a custom deterministic review plus one SDK
`RESULT_VALIDATION/SEMANTIC_REFERENCE` failure projection. Run the new test
before production changes and require it to fail because Coordination is
currently terminal.

- [x] **Step 3b: Restore the existing fail-closed fallback at Coordination**

Modify `backend/runtime/phase_executor.py`. Catch only
`EvidenceContractError` around the unique split-Runtime `synthesize()` call,
construct the existing safe projection:

```python
fallback = AgentsRcaRuntimeResult.validation_failed(
    ResultValidationCategory.SEMANTIC_REFERENCE,
    model_provider=v7_result.run_summary.model_provider,
    model_name=v7_result.run_summary.model_name,
)
self._orchestrator._copy_adaptive_metadata(fallback, v7_result, degraded=True)
v7_result = fallback
```

Continue through the existing atomic SDK projection replacement and
`_record_v5_review` path. Do not retry the model, alter prompts, repair the
invalid root cause, relax Evidence validation, or accept raw model output.

- [x] **Step 3c: Verify the correction before live recovery**

Run:

```powershell
uv run pytest -q tests/runtime/test_phase_executor.py
uv run pytest -q tests/diagnosis/test_orchestrator.py tests/diagnosis/test_agents_runtime.py
uv run ruff check backend/runtime/phase_executor.py tests/runtime/test_phase_executor.py
uv run pytest -q
```

Expected: all commands exit `0`; the new test proves semantic validation uses
the existing safe fallback and no unrelated Runtime or V8.2 behavior regresses.

Status: verified on 2026-07-26. The new test failed on both memory and SQLite
before the production change, then passed `2/2` after the correction. Runtime
Phase tests passed `31/31`; Orchestrator and Agents Runtime tests passed
`214/214`; focused Ruff passed; the full suite passed `1392/1392` with one
pre-existing Starlette/httpx deprecation warning. The correction is committed
as `35df5de731b628339e6e8e19f36c21b7ac8901c4`.

- [x] **Step 3d: Recover only failed Adaptive Telecom:5**

After committing the verified correction, run only Adaptive Telecom:5 from the
frozen batch02 index into a new artifact root and Runtime database. Require a
non-empty evidence-valid prediction, completed Runtime Run, closed Model
lifecycles, no failed Phase, and benchmark/Runtime token equality. Merge that
single replacement row with the preserved original batch02 only after a
read-only audit records both source Run IDs and source hashes.

Recovery status: the targeted Adaptive-only run
`run-20260726T124413016863Z` under
`D:\data\OpenRCA\results-v9-35df5de-batch02-recovery-telecom5-adaptive`
passed its wrapper and an independent read-only audit. It produced one
non-empty evidence-valid prediction with no failed case or read-only violation.
The Runtime Run and Attempt completed; Model lifecycles closed at `11=11+0`;
Phase failures and invalid-reference executions were zero; benchmark and
Runtime tokens matched exactly at `15337/3824`. The prediction SHA-256 is
`472885CAF4C81515E17EB17B73A2F4FB78BCB1A43E9666136C208911DD1ACE25`.
Authoritative merge result:
`run-merged-20260726T130524779541Z` under
`D:\data\OpenRCA\results-v9-35df5de-batch02-merged-audited` retains all eight
Fixed rows and seven original Adaptive rows, replacing only Adaptive Telecom:5.
Independent audit found 16 unique completed Runtime Runs, 8 non-empty
evidence-valid rows per strategy, closed Model lifecycles at Fixed `72=72+0`
and Adaptive `93=93+0`, zero Phase and invalid-reference failures, and exact
Runtime totals: Fixed tokens `89846/22158`, tool calls `32`, duplicate
rejections `0`; Adaptive tokens `122893/36760`, tool calls `113`, duplicate
rejections `19`. Manifest SHA-256 is
`4f079880e9bc503fa6e236991d2fe3ad618595bd53ce89a36065ae05b84a8350`.

The first merge draft `run-merged-20260726T130058592316Z` under
`D:\data\OpenRCA\results-v9-35df5de-batch02-merged` failed independent audit
because the aggregation subtracted failed-run tool metrics that the original
benchmark summary had already recorded as zero. Its predictions, Runtime
identities, and tokens were unaffected. It remains preserved as a rejected
draft and is explicitly referenced by the authoritative manifest; it must not
be used in the final five-batch merge.

- [x] **Step 3e: Correct and recover only the two failed Batch03 strategy rows**

Batch03 original result `run-20260726T132413545410Z` under
`D:\data\OpenRCA\results-v9-35df5de-batch03` is preserved read-only. It contains
16 unique Runtime Runs and Attempts, all completed, with exact benchmark/Runtime
token equality. Fourteen strategy rows are non-empty. The two rejected rows are:

1. Fixed Telecom:9 timed out. The Runtime safely emitted no prediction, but its
   internally expired SDK turn left Coordinator Model execution
   `model-exec-8d85d1308f05436cb4e412a1211b6ed6` started-only. Batch Model lifecycle
   totals are therefore `180 started = 178 completed + 1 failed + 1 open`.
   Root cause: the shared Run deadline marks active Model IDs before cancelling
   the real SDK turn, while `AgentsRcaRuntime._invoke_turn` clears those IDs
   without emitting the known terminal `model.failed` event. External
   cancellation must retain its existing started-only ambiguous semantics.
2. Adaptive Telecom:40 completed all phases and closed every Model lifecycle,
   but the model result failed `semantic_reference` validation. The correction
   from commit `35df5de` worked as designed: the invalid SDK projection was
   rejected atomically and replaced by the existing deterministic review.
   That review had no evidence-supported root cause, so the benchmark correctly
   recorded an empty prediction instead of accepting or fabricating a cause.

After explicit approval, first add one regression test that distinguishes
internal deadline cancellation from external cancellation. Apply the smallest
shared Runtime correction, rerun focused Runtime/Agents tests, Ruff, and the full
suite, then commit. Recover only Fixed Telecom:9 and Adaptive Telecom:40 into
new artifact roots and Runtime databases; do not rerun or overwrite the other
14 accepted strategy rows. Merge only after an independent read-only audit
confirms non-empty evidence-valid replacements, closed Model lifecycles, no
failed Phase, and exact token equality.

Correction status: verified and committed on 2026-07-26 as
`557638a8f33aff9bd574b46665f02621147f057a`. Before the production change, the
new real-SDK deadline test observed only `model.started`; the external
cancellation protection test passed. After the minimal shared correction, both
tests passed. Related Runtime, Orchestrator, and Agents tests passed `247/247`;
focused Ruff passed; the full suite passed `1394/1394` with the single existing
Starlette/httpx deprecation warning. The correction stores the actor for each
active Model request and emits `model.failed` only when that execution ID was
marked by the internal Run deadline. No retry, prompt, timeout, fallback, or
Evidence validation behavior changed. Targeted recovery remains pending.

The two single-case indexes were copied exactly from frozen batch03 and contain
no ground-truth fields:

```text
D:\data\OpenRCA\prepared-v9-0e53c66\recovery-telecom9-fixed-557638a.json
SHA-256 C0ED1690E591A5310458EA4246187131803CAF45C1385CA3AA252CDA2081F89C

D:\data\OpenRCA\prepared-v9-0e53c66\recovery-telecom40-adaptive-557638a.json
SHA-256 8671D55B91F0F42E67FBDCE70F6A6C86B60A9AC5007ECE0036C4644A071FCB8D
```

Use `D:\data\OpenRCA\commands\run-v9-batch03-recovery.ps1` one target at a
time. Its SHA-256 is
`13292F2E433C6AD3FA48C60795E4F004A23E34EC1CEA28EB34F5526A0D3A3254`.
The script was parsed successfully by the PowerShell parser, and its read-only
SQLite audit was exercised against the preserved failed Telecom:9 Runtime Run.
It refuses an unexpected commit or an existing output path, keeps credentials
only in process environment variables, disables tracing and OTel, and gates
prediction, Evidence, Runtime, Attempt, Model, Phase, token, and transcript
integrity before printing `RECOVERY_ACCEPTED`.

Fixed Telecom:9 recovery status: accepted. Run
`run-20260727T110222195921Z` under
`D:\data\OpenRCA\results-v9-557638a-batch03-recovery-telecom9-fixed`
passed its wrapper and an independent read-only audit. The prediction is
non-empty and 100% evidence-valid, with no failed benchmark case or read-only
violation. Runtime Run `cccc82f8-d3b6-4cfe-9354-ba4c1444b514` and its sole
Attempt completed; Model lifecycles closed at `8=8+0`; Phase and
invalid-reference failures are zero; benchmark and Runtime tokens match exactly
at `9239/2100`; the transcript has no connection, timeout, tracing, or executor
warning. Prediction CSV SHA-256 is
`7A3C64866B9750E31DBA86D6B524D98D9F05E38F4DBA85F848585D342C83CDD3`.
Preserve the artifact, Runtime database, and transcript read-only. Only Adaptive
Telecom:40 recovery remains before the Batch03 audited merge.

Adaptive Telecom:40 recovery status: accepted. Run
`run-20260727T111550776549Z` under
`D:\data\OpenRCA\results-v9-557638a-batch03-recovery-telecom40-adaptive`
passed its wrapper and an independent read-only audit. The prediction is
non-empty and 100% evidence-valid, with no failed benchmark case or read-only
violation. Runtime Run `59236360-ad1d-4adf-85fb-2ba2e7e333a2` and its sole
Attempt completed; Model lifecycles closed at `10=10+0`; Phase and
invalid-reference failures are zero; benchmark and Runtime tokens match exactly
at `12654/3274`; the transcript has no connection, timeout, tracing, or executor
warning. Prediction CSV SHA-256 is
`C5B4BE9525E4A668DA4192C858A2D17D7EB7473CFA6A241F64E097782CA80910`.

Batch03 authoritative merge:
`run-merged-20260727T112522188715Z` under
`D:\data\OpenRCA\results-v9-557638a-batch03-merged-audited` retains the 14
accepted original strategy rows and replaces only Fixed Telecom:9 and Adaptive
Telecom:40. An independent audit verified eight non-empty rows per strategy,
frozen case order, partition projections, row-level source equality, all source
and artifact hashes, and 16 unique completed Runtime Runs and Attempts. Model
lifecycles close at Fixed `75=75+0` and Adaptive `104=104+0`; Phase and
invalid-reference failures are zero. Exact Runtime totals are Fixed tokens
`90466/26001`, tool calls `32`, duplicate rejections `0`; Adaptive tokens
`136770/39673`, tool calls `117`, duplicate rejections `12`. Manifest SHA-256 is
`04a22eaab60c5f67591f90c6c067532d911c0dfe691759c7c8d5a86e64aed714`.
The manifest explicitly retains original batch average durations because exact
original per-case `perf_counter` durations were not persisted; both recovery
summaries retain their exact durations. The original run, both recovery runs,
and the authoritative merge must remain read-only.

Batch04 preflight: ready on 2026-07-27. Frozen index
`D:\data\OpenRCA\prepared-v9-0e53c66\batches\batch-04.json` contains the
expected eight unique cases and retains manifest hash
`434e36603328fb5f0ec58730bcde5696931d2267dad99aca448cbfd6b52e812a`;
its file SHA-256 is
`2BE0589A46EDFB97D474F0486359EC22A45653DC1B6BC3F3479FB2BD1D2E77B3`.
The isolated worktree is at correction commit
`557638a8f33aff9bd574b46665f02621147f057a`, and the planned artifact,
Runtime database, and transcript paths were absent before execution. Use
`D:\data\OpenRCA\commands\run-v9-batch04.ps1`; its SHA-256 is
`951FD301FFEA6D7B8585514C71DB0FD9F065B0BBBD6B19F5325860F3D3B3C29B`.
PowerShell parsing and embedded Python audit compilation passed. The script
refuses existing targets and gates frozen identity, case order, predictions,
Runtime and Attempt completion, Model lifecycles, Phase and Evidence integrity,
token and tool accounting, transcript errors, and all 16 unique Runtime IDs.

Batch04 original result: `run-20260727T113302879786Z` under
`D:\data\OpenRCA\results-v9-557638a-batch04` is preserved. All 16 Runtime Runs
and Attempts completed; Model lifecycles close at Fixed `75=69+6` and Adaptive
`91=86+5`; Phase and invalid-reference failures are zero; benchmark and Runtime
tokens match exactly. Ten strategy rows are accepted. Six rows are empty:

1. Fixed cloudbed-1:55 and cloudbed-1:37 ended in `APIConnectionError`.
2. Fixed cloudbed-1:10 produced no evidence-supported root cause after two
   invalid or missing Metric specialist outputs and safely failed
   `missing_root_cause`.
3. Adaptive cloudbed-1:43 ended in `APIConnectionError`; cloudbed-2:62 ended in
   `APITimeoutError`; cloudbed-2:35 exhausted the 180-second Runtime deadline.

The transcript contains ten connection errors, one request timeout, and one
executor shutdown warning. Five failures are therefore external transport or
timeout outcomes; the sixth is stochastic invalid model output handled
fail-closed. No Model lifecycle, Phase, semantic-reference, token-accounting, or
new code defect contributed. The original wrapper's displayed
`FixedNonEmpty/AdaptiveNonEmpty=8` fields were fixed constants; its actual gate
correctly detected the empty rows. Authoritative non-empty counts are `5/8` per
strategy.

Recover only the failed strategy rows in two steps. The Fixed index
`D:\data\OpenRCA\prepared-v9-0e53c66\recovery-batch04-fixed-557638a.json`
contains cloudbed-1:55, :10, and :37 and has SHA-256
`2060A83BFC429B239471AA312B887CBB35AA084B71697810906324CD2AD8158C`.
The Adaptive index
`D:\data\OpenRCA\prepared-v9-0e53c66\recovery-batch04-adaptive-557638a.json`
contains cloudbed-1:43, cloudbed-2:62, and cloudbed-2:35 and has SHA-256
`38B9AE30B6ACDB9C84304231F2ACCD24A918E3D57C2268A3C06F595538BDAD50`.
Both match their frozen batch04 source objects exactly and contain no
ground-truth fields. Use
`D:\data\OpenRCA\commands\run-v9-batch04-recovery.ps1`; PowerShell parsing and
embedded Python audit compilation passed, and its SHA-256 is
`3A56B292D00B103F3E79DC329DB9E6123B2C88C8F0A768A14336FE4820CB5241`.
Run Fixed first, preserve and audit it, then run Adaptive. Do not overwrite or
rerun the original ten accepted strategy rows.

Fixed recovery result: `run-20260727T141509459788Z` under
`D:\data\OpenRCA\results-v9-557638a-batch04-recovery-fixed` is preserved. All
three predictions are non-empty and all Runtime Runs and Attempts completed.
Independent Runtime audit found cloudbed-1:55 closed its Model lifecycle at
`11=11+0` with one root cause, and cloudbed-1:10 at `11=11+0` with twenty root
causes. Those two rows are accepted for the later merge. Cloudbed-1:37 closed
at `8=7+1`; its failed Model call corresponds to the transcript's only
`Connection error`, so the strict zero-transport-error gate rejected that row
without invalidating the other two.

Retry only Fixed cloudbed-1:37 from exact frozen index
`D:\data\OpenRCA\prepared-v9-0e53c66\recovery-batch04-fixed-cloudbed1-37-557638a.json`,
SHA-256
`108FD0C9377BAB86503E6DDF840E061E1CDBD714E6A307AAD48E5FFBF23425FE`.
Use the non-overwriting script
`D:\data\OpenRCA\commands\run-v9-batch04-fixed37-recovery.ps1`, SHA-256
`5FCED1603026C9611DBA621E5689603D88CDFED99A5F530722773EFF21B80444`.
PowerShell parsing, embedded Python compilation, exact source-object equality,
and absent-target checks passed. Do not rerun cloudbed-1:55 or :10.

The isolated cloudbed-1:37 retry produced
`run-20260727T145244979549Z` under
`D:\data\OpenRCA\results-v9-557638a-batch04-recovery-fixed-cloudbed1-37`.
It is preserved and rejected: prediction `{}`, Model lifecycle `8=7+1`, and
one connection error. Runtime audit localizes the failure to `CoordinatorAgent`
round-2 `final_synthesis`; the preceding seven Model calls completed and the
Metric specialist had already produced evidence. The earlier recovery failed
in `conflict_review`, so this is repeated transient transport failure rather
than a deterministic cloudbed-1:37 phase defect.

Do not make a fourth unchanged attempt. V9 still constructs `AsyncOpenAI` with
`max_retries=0`. The proven V8.2 sibling-branch patch
`38b118270542c3449c951b8f8425d618ee44fa59` changes this to bounded
`max_retries=2` and updates its direct unit assertion, but it is not an ancestor
of V9. Port and verify that minimal behavior only after explicit approval, then
commit it and create fresh non-overwriting recovery targets. Preserve every
failed artifact for provenance.

Transport retry port completed at V9 commit
`9018f990d6088ca7fb9efb097d1233693fee1cf2`. TDD first changed the existing
client-construction test and observed the expected `max_retries: 0 != 2`
failure; after the two-line production change, target tests passed `5/5`.
Affected OpenAI Runtime tests passed `121/121`, Ruff passed, and the full suite
passed `1394/1394`; the only warning is the pre-existing Starlette deprecation
warning also present in the clean baseline.

Run the next isolated retry with
`D:\data\OpenRCA\commands\run-v9-batch04-fixed37-recovery-retry.ps1`.
The wrapper locks the previously audited recovery script by SHA-256 and changes
in memory only the expected commit plus the three fresh targets under prefix
`9018f99`. Wrapper SHA-256 is
`510AAC9014722086D2ACF0A78A378372BCB1670B9BC5A05073BCE70A63915259`.
Wrapper and expanded PowerShell parsing, embedded Python compilation, exact
frozen-index equality, worktree commit identity, replacement cardinality, and
absent-target checks passed. Keep all earlier failed artifacts unchanged.

Fixed cloudbed-1:37 bounded-retry result:
`run-20260727T153348311024Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-recovery-fixed-cloudbed1-37`
is accepted. Independent audit confirmed one completed Runtime and Attempt,
three evidence-backed root causes, Model lifecycle `8=8+0`, exact benchmark and
Runtime tokens `9275/2164`, and zero Phase, invalid-reference,
invalid-root-cause-reference, connection, timeout, tracing, executor, or
validation error. Together with cloudbed-1:55 and :10 from the earlier
three-case recovery, all eight Fixed Batch04 rows now have accepted provenance.

Recover only Adaptive cloudbed-1:43, cloudbed-2:62, and cloudbed-2:35 with
`D:\data\OpenRCA\commands\run-v9-batch04-adaptive-recovery-retry.ps1`.
The wrapper reuses the audited three-case script and frozen Adaptive index,
changes only commit and fresh `9018f99` output targets, and has SHA-256
`C20BD794CCA60C96E2BA6D78D44395A945F27F7D80048309B78E67B13444085E`.
Source hash, replacement cardinality, wrapper and expanded PowerShell parsing,
embedded Python compilation, exact source-object equality, commit identity and
absent-target checks passed. Preserve and audit this result before creating the
Batch04 authoritative merge.

Three-case Adaptive recovery result:
`run-20260728T060106949846Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-recovery-adaptive` is preserved and
rejected as a whole. The connectivity smoke succeeded, but the following ten
minutes were a sustained provider outage. Cloudbed-1:43 and cloudbed-2:62 each
closed at `5=0+5`: initial Coordinator, three Specialists, and final
Coordinator all exhausted SDK retries and failed transport. Both predictions
are empty. Cloudbed-2:35 ran after that window and is accepted at row level:
non-empty prediction, one evidence-backed root cause, Model lifecycle
`14=14+0`, and no Model, Phase, reference, timeout, tracing or validation
failure. The process-level executor shutdown warning does not invalidate this
clean row, matching the row-level provenance rule used by earlier audited
merges.

Do not rerun cloudbed-2:35 or all three cases together. Retry only Adaptive
cloudbed-1:43 using
`D:\data\OpenRCA\commands\run-v9-batch04-adaptive-cloudbed1-43-recovery.ps1`.
Its exact frozen index SHA-256 is
`53AA995B39B7E9BAE1D1EBC8A7C17E563ED36F225C82D821A012754A3BF10C02`;
wrapper SHA-256 is
`A4BFABE6CA3755FD83D183E0DCA66681A4F0B830CAADC3E0F1B6E73221265E4F`.
Wrapper and expanded PowerShell parsing, embedded Python compilation, exact
source-object equality, strategy conversion cardinality and absent-target
checks passed. Audit that result before preparing the separate cloudbed-2:62
retry.

Cloudbed-1:43 isolated result:
`run-20260728T070241034277Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-recovery-adaptive-cloudbed1-43`
is preserved and rejected. Five of eight Model calls completed; Deployment
recollection, Coordinator recollection, and final synthesis exhausted retries
and failed transport. The initial concurrent Log, Metric, and Deployment calls
all completed, and the final Coordinator failures were serial, so lowering
parallelism would not address the observed failure. The connectivity smoke ran
approximately five minutes before the first Model phase because telemetry
loading occurred between them; it therefore did not establish endpoint health
for the actual Model window.

This is cloudbed-1:43's third transport-failed execution. Do not make a fourth
unchanged attempt, increase retry count, relax the Evidence gate, or prepare
cloudbed-2:62 against the same unstable upstream. Resume only after switching
to a stable Base URL or confirming sustained recovery of the current endpoint.
The wrapper's final exception text inherited `cloudbed-1:37`; this is a
display-only label defect. Manifest, prediction, strategy, Runtime ID and all
artifact paths correctly identify Adaptive cloudbed-1:43, so preserve the
script and run unchanged for provenance.

The user replaced the Base URL on 2026-07-28. Resume with a fresh,
non-overwriting cloudbed-1:43 target using
`D:\data\OpenRCA\commands\run-v9-batch04-adaptive-cloudbed1-43-stable-recovery.ps1`.
It preserves commit `9018f990d6088ca7fb9efb097d1233693fee1cf2`, the exact frozen
single-case index, Adaptive strategy, 180-second timeout and every strict audit
gate. It changes only the three output targets and corrects the inherited
display label. Wrapper SHA-256 is
`61553426F76F8D8FEC1EDC44A80D9EF219720BA7535F42B64A1D621A37969433`.
Wrapper and expanded PowerShell parsing, embedded Python compilation,
source-object equality, replacement cardinality, worktree identity, corrected
labels and absent-target checks passed. Audit this result before preparing
cloudbed-2:62.

Stable cloudbed-1:43 result: `run-20260728T081643305742Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-recovery-adaptive-cloudbed1-43-stable`
is accepted. Independent audit confirmed one completed Runtime and Attempt,
one evidence-backed root cause, Model lifecycle `11=11+0`, exact benchmark and
Runtime tokens `13366/2786`, and zero Phase, invalid-reference,
invalid-root-cause-reference, connection, timeout, tracing, executor, or
validation error.

The only remaining Batch04 strategy row is Adaptive cloudbed-2:62. Run it with
`D:\data\OpenRCA\commands\run-v9-batch04-adaptive-cloudbed2-62-stable-recovery.ps1`.
The wrapper locks the audited single-case source script by SHA-256, preserves
commit `9018f990d6088ca7fb9efb097d1233693fee1cf2`, and uses the exact frozen
cloudbed-2:62 source object plus fresh non-overwriting targets. Wrapper SHA-256
is `E0B0F7E6E3563FC97A509721F5856C7794DAEE36D8765B50FE1BC19098A73771`;
index SHA-256 is
`BFCBF5EC0DD3F4BB32CFB6CA02E7B85A1EB4366D4E20B07F13DDF8D6F3FC2261`.
Wrapper and expanded PowerShell parsing, embedded Python compilation, exact
source-object equality, replacement cardinality, worktree identity, case and
strategy identity, and absent-target checks passed. Preserve and independently
audit this result before constructing the Batch04 authoritative merge.

Stable Adaptive cloudbed-2:62 result:
`run-20260728T085550561040Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-recovery-adaptive-cloudbed2-62-stable`
is preserved but failed the strict single-row gate. Runtime and Attempt both
completed, while the prediction is empty and no coordination review was
created. Independent Runtime audit localized the only failed Model lifecycle
to round-2 Coordinator `final_synthesis`: the Agents phase began at
`2026-07-28T09:00:53.754167Z`, and the model failed at
`2026-07-28T09:03:53.781585Z`, exhausting the exact frozen 180-second global
deadline. The preceding twelve Model calls completed, including specialist
recollection and conflict review; total lifecycle is `13=12+1`, with exact
tokens `13937/3373`. Phase, invalid-reference, invalid-root-cause-reference,
connection, API-timeout, tracing, executor, and validation errors are zero.

The causal chain is case evidence rather than transport instability: the Log
provider ignored `13168084` malformed telemetry rows, the initial Log and
Deployment specialist results were invalid or missing, Adaptive recollection
added work, and the final synthesis reached the global deadline. This is a
frozen-protocol system outcome. Do not rerun until a favorable sample appears,
and do not raise the timeout for this row alone. The recommended disposition is
to retain its empty prediction in the authoritative Batch04 merge; any
300-second experiment must be labeled supplemental and cannot replace this
formal row without rerunning the complete frozen protocol.

Provenance SHA-256:

```text
adaptive-predictions.csv  7AF7FD152D272DE892F07F7910FDDBE4ED1A3E261AC0213F95C2C542320358A1
run-manifest.json         4E6D9BC5D7BDC299FAA34018130A722BE5FA4655E42FEADD9FBAE68F242596CF
summary.json              A650D00F68DDEC31A8A7A9680020EFC8610F60C84E52B042DAC37466A8B988B1
Runtime database          CC0EA403CE3C2ED180FDB067374E792E35490FEF5A5BDE7A5D252BFE03B9CC48
transcript                FBFA43DCE0D59DA0C9B24BD39A44EEA7730D0F9CFD702363D32FB48B71154BCA
```

The user explicitly approved retaining the frozen timeout outcome. Batch04
authoritative merge `run-merged-20260728T103039795266Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch04-merged-audited` preserves ten
accepted original strategy rows and selects six row-level recovery sources.
Its manifest records all source Run IDs, commits, Runtime databases, source
checksums, row-level Runtime provenance, the timeout disposition, and the
2026-07-28 confirmation.

Independent audit verified the exact frozen case order, partition projections,
source-row equality, all source and artifact checksums, and 16 unique completed
Runtime Runs and Attempts. Fixed has `8/8` non-empty predictions and Adaptive
has `7/8`; only cloudbed-2:62 is empty and listed in `failed_cases`. Evidence
reference validity is `1.0`, with zero Phase or invalid-reference failures.
Model lifecycles close at Fixed `75=75+0` and Adaptive `95=93+2`; the second
Adaptive failure is an original non-empty row that recovered within the same
Run. Exact totals are Fixed tokens `92189/23774`, tool calls `32`, duplicate
rejections `0`; Adaptive tokens `120275/31765`, tool calls `118`, duplicate
rejections `18`. Manifest SHA-256 is
`81f39170a65b4ea5ae1af382e03d73f1611a79b31454b1c695a9450383e3c7b0`.
Merge script
`D:\data\OpenRCA\commands\merge-v9-batch04.py` has SHA-256
`7C63123BC1C51DB9B0F2870D795934AAE3530B7C0F26623AA956328418EDCE4A`.
Preserve every source and merged artifact read-only; proceed to Batch05 without
changing the frozen protocol.

Batch05 preflight: ready on 2026-07-28. Frozen index
`D:\data\OpenRCA\prepared-v9-0e53c66\batches\batch-05.json` contains eight
unique Market/cloudbed-2 cases, is byte-identical to the V8.2 Batch05 index,
retains manifest hash
`434e36603328fb5f0ec58730bcde5696931d2267dad99aca448cbfd6b52e812a`,
and has SHA-256
`8148EABCFD17848078471FFCE8D93A4A3169247555453257946580F74F80BA6F`.
All eight telemetry directories exist and the index contains no forbidden
ground-truth fields.

Run `D:\data\OpenRCA\commands\run-v9-batch05.ps1`. The non-overwriting wrapper
locks the audited Batch04 source script by SHA-256 and changes only the frozen
commit to `9018f990d6088ca7fb9efb097d1233693fee1cf2`, Batch05 index and fresh
artifact/Runtime/transcript paths, displayed batch identity, and the old
display-only hard-coded NonEmpty totals. Both strategies, the 180-second
timeout, concurrency, tracing/OTel settings, and all acceptance gates remain
unchanged. Wrapper and expanded PowerShell parsing, embedded Python
compilation, replacement cardinality, worktree identity, source hash and
absent-target checks passed. Wrapper SHA-256 is
`895915CD890D4266D0FD0D463232F3E8B40C8D08D63C37EE0856BAA271DD2BBB`.
Preserve and independently audit the result before any targeted recovery or
the final five-batch merge.

Batch05 result and audit: run `run-20260728T104608542152Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch05` is preserved. All 16 Runtime Runs
and Attempts completed. Fixed produced `7/8` non-empty rows and Adaptive
produced `6/8`; connection, API-timeout, tracing, Phase, validation, and
invalid-reference errors are all zero. The process emitted one executor
shutdown warning only after all 16 Runs completed, so it does not invalidate
row-level Runtime provenance.

The three empty rows are frozen-protocol outcomes rather than transport
failures:

1. Fixed Market/cloudbed-2:70 exhausted the 180-second Agents deadline during
   Coordinator round-2 specialist collection; Model lifecycle `7=6+1`.
2. Adaptive Market/cloudbed-2:48 exhausted the same deadline during
   Coordinator round-2 final synthesis; Model lifecycle `16=15+1`.
3. Adaptive Market/cloudbed-2:70 stopped as `budget_exhausted` after
   Coordinator round-1 specialist recollection reached the same deadline;
   Model lifecycle `11=9+2`.

All other 13 strategy rows are non-empty, include a review, and have closed
Model lifecycles. Retaining the three empty rows is the recommended formal
disposition: rerunning only failures until success or changing their timeout
would bias the frozen baseline. If non-empty output is mandatory, define a new
timeout/budget protocol and rerun the complete 40-case V9 workflow.

Provenance SHA-256:

```text
fixed-predictions.csv     2C1811B6F57D0B04382D2846AF03C746201DB771749C9168F14F38666D6E07B5
adaptive-predictions.csv  F75340D32304220CAE1CB3EB46DCFC35B9A704BB10D28225458600AAA66F0B5C
run-manifest.json         CC2944609E546149B541CD5305C277EA734E1AD0BF200D8F060F7D14F62CB99E
summary.json              E83728189D4EF85A7C9C5726CB75A3CC6F40D652CEA97636F674D447C9EB2370
Runtime database          3BDB2DCCCDB119D8B408034DD6FDDB5F363436C98A23201CD2511658D8BBFE05
transcript                D07C3403FE6BB2FEEFDD4131BB9C14BA233A6832BD4C29BC91D0456E83E7D857
```

The user explicitly approved retaining all three empty results. Batch05
authoritative merge `run-merged-20260728T130642194648Z` under
`D:\data\OpenRCA\results-v9-9018f99-batch05-merged-audited` preserves every
source CSV byte-for-byte and records each selected Runtime Run plus all three
formal failure dispositions. Independent audit verified frozen case order,
all artifact checksums, 16 unique completed Runtime Runs and Attempts, Fixed
`7/8` and Adaptive `6/8` non-empty predictions, 100% Evidence reference
validity, zero invalid references and read-only violations, and the exact
approved failure set.

Authoritative merge SHA-256:

```text
run-manifest.json         A927F0B74CEC8DB5AAC7A12B5AFB20F37DB838738D85C8678A966831039D4F2F
summary.json              6C03E145A2B4DDABCB76A1E0FD7D8AE3ADD25745E4B7CE1E5F16D2CB7AC530F4
fixed-predictions.csv     2C1811B6F57D0B04382D2846AF03C746201DB771749C9168F14F38666D6E07B5
adaptive-predictions.csv  F75340D32304220CAE1CB3EB46DCFC35B9A704BB10D28225458600AAA66F0B5C
merge script              1905A694F7C1BC3210DA11A4488C51326132688D3568BC6B88CC3B64CC4A28F9
```

The merge contract test first failed because the script did not exist, then
passed after the minimal implementation. Preserve the original and merged
Batch05 artifacts read-only. Proceed to the five-batch authoritative merge and
evaluator gate.

Expected:

```text
40 Fixed predictions
40 Adaptive predictions
Adaptive completion >= 95%
Evidence reference validity = 100%
Read-only violations = 0
Upstream official Adaptive partial score >= frozen V8.2 baseline
```

Five-batch merge and evaluator result: authoritative run
`run-merged-20260728T131350272103Z` under
`D:\data\OpenRCA\results-v9-final-9018f99-merged-audited` concatenates the five
audited batch artifacts without changing any selected row. Independent audit
verified exact source-row equality and frozen order, 40 unique cases per
strategy, 80 unique completed Runtime Runs and Attempts, Fixed `39/40` and
Adaptive `37/40` non-empty predictions, 100% Evidence reference validity, zero
invalid references and read-only violations, and the exact four retained
formal failures. Both strategy summaries record `40/40` completed.

The compatible evaluator produced 80 report rows. Four separate official query
subsets contain ten rows each and match the frozen partition cases after the
required `row_id` ordering. To minimize download size, only
`main/evaluate.py` was sparsely fetched from Microsoft OpenRCA commit
`c1bd4af7f635171a1c31cdd567c07d698dff6abc`. Before using it for V9, the same
evaluator reprocessed V8.2: all 80 report rows were semantically identical
after normalizing the upstream evaluator's unordered `set` rendering, and
strict and partial totals remained zero.

V9 compatible and upstream official results both report Fixed and Adaptive
strict and partial scores of `0`. The required Adaptive official partial score
is therefore equal to, not below, the frozen V8.2 baseline. This passes the
comparison gate but provides no evidence of an accuracy improvement.

Final SHA-256:

```text
run-manifest.json         66AB4BD47C80027AE0037990EAE17DBDF89833EEF4A97593EED4A897ACF33213
summary.json              52B43BD7196C76DBFFDCC65647D49C5E2FDAA60332E493AD8E5B4A80D9986F87
fixed-predictions.csv     0A10B5329ED4577DF0DD92B0C8DCAD720A5137C65ED9EAB472971EFC4BACFA1A
adaptive-predictions.csv  F3C597DA723D808B83C23B5A246397964AD6E1C8593B01B7D776B1139FFA1DCE
compatible-report.csv     DF38537C7AC9512BE6C77D93CBBB88D298A2B98DE782E6EB5606D8E3AED10621
official-report.csv       695894AA795FF66A25145684C33B09D60E59A45D7B40B72935F82B73AD5D158C
merge script              D335E60FCA5D8E33BAD4B0BB265F0FBA951DE8C971DA2D64AB8344F45702497C
official evaluate.py      9A1C235139266C32B0E95C7E9938591B8FE014AAD4CDE95580F73F13A54D53F1
```

The merge contract test first failed because the final script did not exist,
then passed after the minimal implementation. Preserve the five batch sources,
final merge, official query subsets, sparse evaluator checkout, and V8.2
validation report read-only. Proceed to real-Run Replay and Diff verification.

- [ ] **Step 4: Verify Replay and Diff against real frozen Runs**

Replay at least one Fixed and one Adaptive Runtime Run from the 40-case workflow. Assert zero external calls and attach their `ReplayReport` identifiers. Generate a Diff for the paired Runs and verify stable hashes on two reads.

Status: blocked by real frozen Runtime evidence.

The verification used a byte-identical copy of
`D:\data\OpenRCA\runtime-v9-9018f99-batch05.db`; the frozen source retained
SHA-256
`3BDB2DCCCDB119D8B408034DD6FDDB5F363436C98A23201CD2511658D8BBFE05`.
Market/cloudbed-2:52 Fixed Replay report
`cf57e938-da41-49e3-9dfb-9100a548d8c8` is valid with zero external calls and
no validation errors. Adaptive report
`34b6f836-0aa9-4b9d-85fa-1564a00c6261` also made zero external calls but is
invalid with `illegal_agent_transition`, `illegal_phase_transition`,
`illegal_attempt_transition`, and `illegal_tool_transition`.

The result is systemic rather than case-specific: a read-only replay scan of
all selected Runtime streams found `40/40` Fixed valid and `0/40` Adaptive
valid. Every affected Adaptive stream contains `tool.started` events whose
tool coroutine is cancelled by the SDK without a persisted terminal tool
event. `AgentsRcaRuntime` subsequently emits `agent.failed`, while the open
Tool lifecycle remains. Later Phase and Attempt completion events therefore
cannot be replayed as a valid ordered lifecycle. The durable Tool record is
eventually changed to interrupted at terminal Run commit, but this does not
repair the missing ordered terminal event.

Root cause is the cancellation boundary in
`backend/diagnosis/adaptive_tools.py`: `AdaptiveToolSession` handles
`TimeoutError` and ordinary `Exception`, but `asyncio.CancelledError` bypasses
both and leaves the earlier persisted Tool start open. Do not weaken Replay
validation or edit frozen Runtime data. The minimal implementation correction
must persist a terminal Tool result before re-raising cancellation, with a
regression test that proves `tool.started` and its terminal event are balanced
before `agent.failed`.

The first correction was committed as `bd79bc7`. Its direct cancellation test
passed, but the fresh live smoke Run `run-20260728T134952508920Z` disproved the
fix under the real SDK timeout race: the Run completed with a non-empty,
evidence-valid prediction and balanced Model lifecycle, while read-only audit
still found six `tool.started` events and no terminal Tool event. All six Tool
records were changed to `interrupted` only by terminal Run commit. Replay was
not executed. The source smoke DB SHA-256 before Replay was
`1253997E2DDFB00D9F1984C8CBED00EE771EF9879A386642AA22660C2C599071`.

The second red test reproduced cancellation arriving while internal timeout
cleanup was awaiting terminal persistence. Commit `ba654dd` now completes that
terminal commit in a shielded task before propagating cancellation. Both
cancellation regressions pass; related Adaptive Tool, Runtime idempotence,
Replay, and Diff tests pass `97/97`; Ruff passes; the full suite passes
`1396/1396` with one pre-existing Starlette deprecation warning. Frozen
Runtime databases and the rejected `bd79bc7` smoke remain unchanged.

The replacement non-overwriting smoke command is
`D:\data\OpenRCA\commands\run-v9-adaptive-replay-smoke-ba654dd.ps1`. It runs
Adaptive `Market/cloudbed-2:52`, requires exact started/terminal ToolCall
balance plus at least one terminal failure event, then performs Replay and
requires a valid report with zero external calls. PowerShell parsing,
replacement dry-run, commit, input, and target non-existence checks passed.
Script SHA-256:
`10B2AF85D407A101102269DE5118882892C305F8806A15A3872BBAD6E1495043`.

The `ba654dd` live smoke Run `run-20260728T142243886728Z` disproved that second
fix as the full solution. It completed non-empty and evidence-valid but still
recorded Tool lifecycle `6/0`. Replay report
`04504ab1-994f-49bc-aad7-129c2b2a8131` made zero external calls and correctly
failed with the same four lifecycle errors. Evidence:
`D:\data\OpenRCA\replay-v9-ba654dd-cloudbed2-52\result.json`, SHA-256
`0DF71FAE62267E9007974DE4D1C673279879A8E38E0E84D00B38CD8E2C76BD0E`.
This proves the SDK can abandon child Tool tasks without awaiting their
coroutine cleanup before the Agent becomes terminal.

The third red test therefore moves the invariant to the durable parent
boundary. Commit `ef2d95f` makes `RuntimeCoordinator` close every still-running
ToolCall for an Agent before emitting that Agent's `completed` or `failed`
event. Memory and SQLite tests now prove ordered
`agent.started → tool.started → tool.failed → agent.failed`. Related tests pass
`99/99`; Ruff passes; the full suite passes `1398/1398` with the same
pre-existing Starlette warning. The next non-overwriting smoke command is
`D:\data\OpenRCA\commands\run-v9-adaptive-replay-smoke-ef2d95f.ps1`; parsing,
replacement dry-run, commit, input, and target checks passed. Script SHA-256:
`F626E3A787F9ACC8D4D8E59CFEA79F8FEBD61226BF54D440B3BE25176DD9AC35`.

The `ef2d95f` live smoke Run `run-20260729T000031979842Z` produced an empty
Adaptive result and therefore stopped before Replay. Its read-only Runtime
audit nevertheless found the remaining root cause: `specialist_analysis`
persisted four `agent.started` and four `tool.started` events, but only
Coordinator received a terminal Agent event before `phase.completed`. The
Agent-terminal hook could not close children when the SDK omitted the parent
terminal event itself.

Commit `e8391be` moves the final invariant to the shared phase boundary. Before
`PhaseCommit`, `RuntimeCoordinator` now closes every Agent still open in the
current Attempt; the existing Agent hook closes its Tool children first. A
memory-and-SQLite red-green test proves ordered
`agent.started → tool.started → tool.failed → agent.failed → phase.completed`.
Related Runtime, Tool idempotence, and Replay tests pass `107/107`; Ruff
passes; the full suite passes `1400/1400` with the same pre-existing Starlette
warning. The next non-overwriting smoke uses historically stable Bank:51:
`D:\data\OpenRCA\commands\run-v9-adaptive-replay-smoke-e8391be-bank51.ps1`.
PowerShell parsing and replacement dry-run passed; script SHA-256
`99208F76B6DE13C5C25FFABB889B75DF27B41566FB8D5360573FDF8E614B9239`.

The first `e8391be` Bank:51 Run `run-20260729T021612765011Z` did not exercise
Tool lifecycle or Replay because all five Model calls failed at the Provider
connection boundary, leaving tokens `0/0` and an empty prediction. Preserve
the artifact and do not overwrite it. The live stream still verifies the new
parent closure for its exercised path: Agent lifecycle is `5/5`, Model
lifecycle is `5/5`, all parent terminals precede Phase completion, and
read-only Replay event, checkpoint, and business validation each report no
errors. The source DB SHA-256 before any Replay write is
`F05FA69449DBA0DB23BC2C1065688EA8F5C976C2D692A0F1D299EE3719C420B9`.

Before spending another live Run, execute the two-wave, three-concurrent
Provider preflight
`D:\data\OpenRCA\commands\test-v9-provider-concurrency.ps1`, SHA-256
`C67B53B8F3BB6D8D0DD41FAA3876AACC573B25D0FE2848BC490167AAF046E7C9`.
Only a `6/6` result permits the non-overwriting retry
`D:\data\OpenRCA\commands\run-v9-adaptive-replay-smoke-e8391be-bank51-retry01.ps1`,
SHA-256
`6199922CB491F429F58763481780113A230F2BC7F06F2699F560F11CC38CA600`.
Both scripts pass PowerShell parsing; the retry also passes replacement,
commit, input, and target non-existence checks.

Retry01 Run `run-20260729T030255572555Z` reached real Tool activity with zero
connection errors, but failed in `specialist_analysis` before the first
terminal Tool event. The benchmark retained only
`AttributeError: benchmark case failed`. A temporary diagnostic copy restored
the exact pre-terminal boundary and reproduced the hidden exception:
`_persist_tool_result` indexed existing `ProviderResult` values by `item.id`,
but that domain type has no `id`. Earlier lifecycle fixtures had empty
Provider results, so they did not exercise the faulty merge.

Commit `bcbcafc` replaces that invalid key with the ProviderResult serialized
value, matching the existing value-based deduplication contract without a new
abstraction. The regression fixture now includes a real existing
ProviderResult and passes on Memory and SQLite. Related tests pass `107/107`,
Ruff passes, and the full suite passes `1400/1400` with the same pre-existing
Starlette warning. Retry01 DB SHA-256 is
`21D0D481C80EE4AFF78C814BCB176F4535604A8A9888B0D46CA95ED675C1B25F`.
The next non-overwriting command is
`D:\data\OpenRCA\commands\run-v9-adaptive-replay-smoke-bcbcafc-bank51-retry02.ps1`;
parsing, replacement dry-run, commit, input, and target checks passed. Script
SHA-256:
`787E364EFABF578F517D2F20C40AAE2CEFBD7F254F7838FDF39C871055C680B1`.

Retry02 benchmark Run `run-20260729T042948233434Z` retained an empty prediction
because three Provider connections failed and Deployment recollection emitted
one invalid semantic reference. Those are model/upstream quality failures, not
another durable lifecycle defect: source Runtime Run
`4558eee0-5fdc-4491-a900-26642a68d87a` completed with Agent `11/11`, Model
`14/14`, and Tool `6/6`. Every Tool start has an ordered terminal event,
including two interrupted calls emitted by the phase boundary. No invalid
reference was persisted, and read-only event, checkpoint, and business Replay
validators all return no errors.

Full Replay on the database copy is valid with zero external calls,
no validation errors, and completed Replay Run
`36fa2380-d6f6-489f-9080-3537211c2ca7`. Replay report:
`a74fb5a4-93b0-4c4d-a11a-afbe13c7bd30`. Source DB SHA-256:
`FE388E715FF719747B3F83ABB78B1A6FAC1A61A7E02366A1EB5AEDC93A93A303`.
Audit copy:
`D:\data\OpenRCA\replay-v9-bcbcafc-bank51-retry02-audit\runtime-audit.db`,
SHA-256
`C4D706DBBCAE1C89727FFCAD6232DB9C46C1BCC016A9FBA37AE2F2F38BE502C1`.
Adaptive Replay lifecycle verification is complete; preserve the rejected
business result as Provider/model quality risk rather than rerunning it.

The source hash above was captured immediately after the run. Opening the
source through the application SQLite stack for validator access checkpointed
physical pages, changing its current file SHA-256 to
`3C2A54CFBE8EB4CD6892B99E6BCE661E47EB54905B07F2574AFCA9243DFE2DF1`.
A subsequent true SQLite `mode=ro` audit confirms logical content was not
extended: exactly one Live Run, one Attempt, 90 events, eight checkpoints, and
no Replay Run or report. Treat this physical hash change as an audit-process
residual risk; do not describe the application-stack validation open as
byte-read-only.

Run Diff itself is deterministic for supported same-Investigation inputs.
Fixed source-to-Replay canonical SHA-256 repeated twice as
`D9D140A4A4244C3454279DC6F9C07C81BF7D8B6855DBA92583929E8D350B74BE`;
Adaptive source-to-failed-Replay repeated twice as
`FDE0D27233D36A8C06ABECBED47B7F25DE3E15057D2B2E992A602A654D1F6B24`.
The 80 selected benchmark Runs belong to 80 separate Investigations, so direct
Fixed/Adaptive Diff is rejected by the approved same-Investigation contract.
Meeting a cross-strategy benchmark Diff expectation would require a separately
approved benchmark-specific comparison contract.

On 2026-07-29 the user explicitly confirmed retaining the approved
same-Investigation contract. Cross-Investigation benchmark comparison is not a
V9 requirement. This closes the Diff gate using the repeated hashes above.

Evidence:
`D:\data\OpenRCA\replay-diff-v9-final-9018f99-cloudbed2-52\result.json`,
SHA-256
`5F59E3230259B625BF3D912E0C0D3D90EF93E7BF59264512F36870BC1226F504`.
The earlier Bank:51 diagnostic copy is retained at
`D:\data\OpenRCA\replay-diff-v9-final-9018f99-bank51\runtime-audit.db`.

Any code correction after this point changes the release commit and requires
fresh affected V9 verification evidence before completion.

- [x] **Step 5: Record release evidence and residual risk**

Record exact command results, pytest count, build result, Runtime acceptance artifact path, OpenRCA run ID, official report path, V8.2 baseline comparison, performance report, skipped checks, and unresolved risks in `docs/superpowers/current.md`. If credentials or the full dataset are unavailable, keep V9 at `verifying` and state that the live release gate is blocked; do not claim completion.

Status: verified. Completion commit `bcbcafc` passes Ruff, `1400/1400` pytest,
the frontend production build, and all 14 key-free Runtime acceptance
scenarios. Acceptance artifact:
`C:\Users\林佳威\.codex\worktrees\b551\sre-agent\output\runtime-acceptance\runtime-20260729T052807237954Z-790b99ae\result.json`,
SHA-256
`F712B7E9B592DBE2B8DBFF1794B4517F5D3F051C39D172C3FD217F7643D4EAA4`.
V9 does not improve official OpenRCA accuracy over V8.2: both score zero
strict and partial. It does improve Adaptive Evidence reference validity from
`65.615%` with 32 invalid references to `100%` with zero invalid references
and adds durable Runs, recovery, Replay, and deterministic supported Diff.
Residual risks are Provider instability, model semantic-reference failures,
timeouts and empty results, Runtime enabled p50 overhead of `+45.237%` versus
V8.2-compatible sync, higher 40-case duration, the source SQLite physical-hash
checkpoint caveat, and the fact that the formal 40-case scoring artifact is
commit `9018f99` while post-artifact lifecycle corrections are verified by
fresh targeted and platform-wide evidence at `bcbcafc`.

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
