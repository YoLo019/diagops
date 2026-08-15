import asyncio
import json
from datetime import UTC, datetime

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.agents_runtime import AgentsRcaRuntime, TokenBudgetExceeded
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phases import (
    V10_PHASE_ORDER,
    BusinessMutation,
    PhaseCommit,
    PhaseOutput,
    ToolCommit,
    checkpoint_digest,
)
from backend.runtime.store import InMemoryRuntimeStore, RuntimeConflict
from backend.runtime.writer import RuntimeEventCommand, RuntimeWriter
from backend.tools.registry import ToolRegistry


class RecordingPhaseExecutor:
    def __init__(self, investigation_id: str) -> None:
        self.investigation_id = investigation_id
        self.phases: list[RuntimePhase] = []

    async def execute_phase(self, phase_input) -> PhaseOutput:
        self.phases.append(phase_input.phase)
        status = (
            "skipped"
            if phase_input.phase == RuntimePhase.CONFLICT_REVIEW
            else "completed"
        )
        return PhaseOutput(
            business_mutation=BusinessMutation(
                investigation_id=self.investigation_id
            ),
            status=status,
            safe_payload={"status": status},
            resume_state=phase_input.resume_state,
        )


class BlockingPhaseExecutor(RecordingPhaseExecutor):
    def __init__(self, investigation_id: str) -> None:
        super().__init__(investigation_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_phase(self, phase_input) -> PhaseOutput:
        self.phases.append(phase_input.phase)
        self.started.set()
        await self.release.wait()
        return PhaseOutput(
            business_mutation=BusinessMutation(
                investigation_id=self.investigation_id
            ),
            status="completed",
            safe_payload={"status": "completed"},
            resume_state=phase_input.resume_state,
        )


def _services():
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-1",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="runtime",
                description="runtime",
                started_at=datetime(2026, 7, 17, tzinfo=UTC),
            ),
        )
    )
    store = InMemoryRuntimeStore(repository, lease_seconds=30)
    run = store.create_run(
        RuntimeRun(
            id="run-1",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    executor = RecordingPhaseExecutor("inv-1")
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )
    return store, run, executor, coordinator


def _interrupted_run_with_remaining_budget(
    runtime_store,
    *,
    post_checkpoint_increment: bool = False,
    tool_budget: int = 5,
    token_budget: int = 10,
):
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id="run-resume-budget",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            tool_budget=tool_budget,
            token_budget=token_budget,
        )
    )
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    call = ToolCallRecord(
        id="tool-budget-1",
        task_id="task-budget",
        agent_name="LogAgent",
        tool_name="read_logs",
        status=ToolCallStatus.SUCCESS,
        runtime_run_id=run.id,
        logical_call_id="LogAgent:1:1",
        idempotency_key="budget-key-1",
        execution_id="tool-exec-budget-1",
    )
    store.commit_tool(
        ToolCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            business_mutation=BusinessMutation(
                investigation_id="inv-1", tool_calls=(call,)
            ),
            call=call,
        )
    )
    store.append_event_command(
        RuntimeEventCommand(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            event_type=RuntimeEventType.MODEL_COMPLETED,
            actor_type=RuntimeActorType.MODEL,
            execution_id="model-exec-budget-1",
            safe_payload={
                "status": "completed",
                "input_tokens": 4,
                "output_tokens": 2,
            },
        )
    )
    checkpoint = store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(
                remaining_tool_budget=max(0, tool_budget - 1),
                remaining_token_budget=max(0, token_budget - 6),
                successful_tool_keys=["budget-key-1"],
            ),
        )
    )
    if post_checkpoint_increment:
        late_call = call.model_copy(
            update={
                "id": "tool-budget-2",
                "logical_call_id": "LogAgent:1:2",
                "idempotency_key": "budget-key-2",
                "execution_id": "tool-exec-budget-2",
            }
        )
        store.commit_tool(
            ToolCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=leased.lease_version,
                business_mutation=BusinessMutation(
                    investigation_id="inv-1", tool_calls=(late_call,)
                ),
                call=late_call,
            )
        )
        store.append_event_command(
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=leased.lease_version,
                event_type=RuntimeEventType.MODEL_COMPLETED,
                actor_type=RuntimeActorType.MODEL,
                execution_id="model-exec-budget-2",
                safe_payload={
                    "status": "completed",
                    "input_tokens": 1,
                    "output_tokens": 0,
                },
            )
        )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    return run, checkpoint


@pytest.mark.anyio
async def test_resume_phase_input_uses_checkpoint_remaining_budgets(runtime_store) -> None:
    store = runtime_store
    run, _checkpoint = _interrupted_run_with_remaining_budget(store)

    class BudgetExecutor:
        def __init__(self) -> None:
            self.inputs = []

        async def execute_phase(self, phase_input) -> PhaseOutput:
            self.inputs.append(
                (phase_input.tool_budget, phase_input.token_budget)
            )
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    executor = BudgetExecutor()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert executor.inputs
    assert set(executor.inputs) == {(4, 4)}
    assert store.list_checkpoints(run.id)[-1].resume_state == RuntimeResumeState(
        remaining_tool_budget=4,
        remaining_token_budget=4,
        successful_tool_keys=["budget-key-1"],
    )
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_before_first_checkpoint_uses_frozen_initial_budgets(
    runtime_store,
) -> None:
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id="run-resume-before-checkpoint",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            tool_budget=5,
            token_budget=10,
        )
    )
    leased, _attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )

    class BudgetExecutor:
        def __init__(self) -> None:
            self.inputs = []

        async def execute_phase(self, phase_input) -> PhaseOutput:
            self.inputs.append(
                (phase_input.tool_budget, phase_input.token_budget)
            )
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state.model_copy(
                    update={
                        "remaining_tool_budget": phase_input.tool_budget,
                        "remaining_token_budget": phase_input.token_budget,
                    }
                ),
            )

    executor = BudgetExecutor()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert set(executor.inputs) == {(5, 10)}
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_deducts_post_checkpoint_durable_usage_once(runtime_store) -> None:
    store = runtime_store
    run, _checkpoint = _interrupted_run_with_remaining_budget(
        store, post_checkpoint_increment=True
    )

    class DurableIncrementExecutor:
        def __init__(self) -> None:
            self.inputs = []

        async def execute_phase(self, phase_input) -> PhaseOutput:
            self.inputs.append(
                (phase_input.tool_budget, phase_input.token_budget)
            )
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state.model_copy(
                    update={
                        "remaining_tool_budget": phase_input.tool_budget,
                        "remaining_token_budget": phase_input.token_budget,
                        "successful_tool_keys": ["budget-key-1", "budget-key-2"],
                    }
                ),
            )

    executor = DurableIncrementExecutor()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert set(executor.inputs) == {(3, 3)}
    final_state = store.list_checkpoints(run.id)[-1].resume_state
    assert final_state.remaining_tool_budget == 3
    assert final_state.remaining_token_budget == 3
    assert final_state.successful_tool_keys == ["budget-key-1", "budget-key-2"]
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_exhausted_budget_blocks_model_and_tool_boundaries(
    runtime_store,
) -> None:
    store = runtime_store
    run, _checkpoint = _interrupted_run_with_remaining_budget(
        store,
        tool_budget=1,
        token_budget=6,
    )
    external_calls = {"model": 0, "tool": 0}

    class BoundaryExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            async def turn(**_kwargs):
                external_calls["model"] += 1
                raise AssertionError("exhausted model budget crossed boundary")

            runtime = AgentsRcaRuntime(
                model="fake",
                turn=turn,
                runtime_token_budget=phase_input.token_budget,
            )
            with pytest.raises(TokenBudgetExceeded):
                await runtime._invoke_turn(None, model=runtime.model)

            registry = ToolRegistry()

            def invoke_tool(**_kwargs):
                external_calls["tool"] += 1
                raise AssertionError("exhausted tool budget crossed boundary")

            registry.register(
                ToolSpec(name="read_logs", description="Read logs."),
                invoke_tool,
            )
            session = AdaptiveToolSession(
                event=store.investigation_repository.get("inv-1").event,
                seed_evidence=[],
                registry=registry,
                task_ids={AgentName.LOG: "task-budget"},
                max_total_tool_calls=phase_input.tool_budget,
            )
            result = json.loads(
                await session.invoke(
                    AgentName.LOG,
                    "read_logs",
                    json.dumps(
                        {
                            "start_time": "2026-07-16T23:55:00+00:00",
                            "end_time": "2026-07-17T00:05:00+00:00",
                            "reason": "verify resumed budget boundary",
                        }
                    ),
                    1,
                )
            )
            assert result["stop_reason"] == "budget_exhausted"
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=BoundaryExecutor(),
        heartbeat_seconds=1,
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert external_calls == {"model": 0, "tool": 0}
    final_state = store.list_checkpoints(run.id)[-1].resume_state
    assert final_state.remaining_tool_budget == 0
    assert final_state.remaining_token_budget == 0
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_execute_commits_every_phase_and_completes_attempt() -> None:
    store, run, executor, coordinator = _services()

    await coordinator.execute(run.id, owner="worker-a")

    assert executor.phases == list(V10_PHASE_ORDER)
    assert store.get_run(run.id).status == RuntimeRunStatus.COMPLETED
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.COMPLETED
    assert [item.completed_phase for item in store.list_checkpoints(run.id)] == list(
        V10_PHASE_ORDER
    )
    conflict_events = [
        item.event_type.value
        for item in store.list_events(run.id)
        if item.phase == RuntimePhase.CONFLICT_REVIEW
    ]
    assert "phase.skipped" in conflict_events
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_execute_persists_split_model_token_usage() -> None:
    store, run, _executor, coordinator = _services()

    class ModelUsageExecutor(RecordingPhaseExecutor):
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.SPECIALIST_ANALYSIS:
                await phase_input.persist_model_event(
                    "model-usage-1",
                    "completed",
                    4,
                    2,
                    "LogAgent",
                )
            return await super().execute_phase(phase_input)

    coordinator.phase_executor = ModelUsageExecutor("inv-1")

    await coordinator.execute(run.id, owner="worker-a")

    model_event = next(
        item
        for item in store.list_events(run.id)
        if item.event_type == RuntimeEventType.MODEL_COMPLETED
    )
    assert model_event.safe_payload == {
        "status": "completed",
        "input_tokens": 4,
        "output_tokens": 2,
    }
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_cancelled_run_is_terminal_and_cannot_resume() -> None:
    store, run, _executor, coordinator = _services()
    await coordinator.request_cancel(run.id)

    assert store.get_run(run.id).status == RuntimeRunStatus.CANCELLED
    with pytest.raises(RuntimeConflict):
        await coordinator.resume(run.id, owner="worker-b")
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_running_cancel_discards_inflight_phase_result_at_safe_boundary() -> None:
    store, run, _executor, coordinator = _services()
    executor = BlockingPhaseExecutor("inv-1")
    coordinator.phase_executor = executor

    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await executor.started.wait()
    await coordinator.request_cancel(run.id)
    executor.release.set()

    result = await execution

    assert result.status == RuntimeRunStatus.CANCELLED
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.CANCELLED
    assert store.list_checkpoints(run.id) == []
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "run.cancelled"
    ) == 1
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_rejects_tampered_checkpoint_and_keeps_interrupted() -> None:
    store, run, _executor, coordinator = _services()
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(),
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    checkpoint = store.list_checkpoints(run.id)[0]
    store._checkpoints[checkpoint.id] = checkpoint.model_copy(
        update={"state_digest": "0" * 64}
    )

    with pytest.raises(RuntimeConflict, match="checkpoint"):
        await coordinator.resume(run.id, owner="worker-b")

    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    assert len(store.list_attempts(run.id)) == 1
    assert store.list_events(run.id)[-1].event_type.value == "recovery.rejected"
    await coordinator.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed_evidence_ids", ["missing-evidence"]),
        ("completed_finding_ids", ["missing-finding"]),
        ("completed_review_ids", ["missing-review"]),
        ("completed_report_ids", ["missing-report"]),
        ("successful_tool_keys", ["missing-tool-key"]),
        ("remaining_tool_budget", 1),
        ("remaining_token_budget", 1),
    ],
)
async def test_resume_rejects_semantically_invalid_checkpoint_state(
    field: str, value: object
) -> None:
    store, run, executor, coordinator = _services()
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(),
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    checkpoint = store.list_checkpoints(run.id)[0]
    state = checkpoint.resume_state.model_copy(update={field: value})
    store._checkpoints[checkpoint.id] = checkpoint.model_copy(
        update={
            "resume_state": state,
            "state_digest": checkpoint_digest(
                run_id=checkpoint.run_id,
                attempt_id=checkpoint.attempt_id,
                completed_phase=checkpoint.completed_phase,
                resume_state=state,
            ),
        }
    )
    if field == "remaining_tool_budget":
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"tool_budget": 0}
        )
    if field == "remaining_token_budget":
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"token_budget": 0}
        )

    with pytest.raises(RuntimeConflict, match="checkpoint"):
        await coordinator.resume(run.id, owner="worker-b")

    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    assert len(store.list_attempts(run.id)) == 1
    assert executor.phases == []
    assert store.list_events(run.id)[-1].event_type.value == "recovery.rejected"
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_creates_new_attempt_and_starts_after_complete_checkpoint() -> None:
    store, run, executor, coordinator = _services()
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(),
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert executor.phases == list(V10_PHASE_ORDER)[1:]
    assert [item.status for item in store.list_attempts(run.id)] == [
        RuntimeAttemptStatus.INTERRUPTED,
        RuntimeAttemptStatus.COMPLETED,
    ]
    recovery_events = [
        item.event_type.value
        for item in store.list_events(run.id)
        if item.event_type.value.startswith("recovery.")
    ]
    assert recovery_events == ["recovery.started", "recovery.completed"]
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_concurrent_resume_has_exactly_one_winner() -> None:
    store, run, _executor, coordinator = _services()
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(),
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )

    results = await asyncio.gather(
        coordinator.resume(run.id, owner="worker-b"),
        coordinator.resume(run.id, owner="worker-c"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RuntimeConflict) for item in results) == 1
    assert len(store.list_attempts(run.id)) == 2
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_concurrent_resume_loser_never_observes_initial_budget(
    runtime_store,
) -> None:
    store = runtime_store
    run, _checkpoint = _interrupted_run_with_remaining_budget(store)

    class BlockingBudgetExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.inputs = []

        async def execute_phase(self, phase_input) -> PhaseOutput:
            self.inputs.append(
                (phase_input.tool_budget, phase_input.token_budget)
            )
            self.started.set()
            await self.release.wait()
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    executor = BlockingBudgetExecutor()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )
    tasks = [
        asyncio.create_task(coordinator.resume(run.id, owner="worker-b")),
        asyncio.create_task(coordinator.resume(run.id, owner="worker-c")),
    ]
    await executor.started.wait()
    executor.release.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RuntimeConflict) for item in results) == 1
    assert executor.inputs
    assert set(executor.inputs) == {(4, 4)}
    assert len(store.list_attempts(run.id)) == 2
    await coordinator.shutdown()


def test_expired_lease_audit_interrupts_without_automatic_resume() -> None:
    store, run, executor, coordinator = _services()
    leased, _attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store._runs[run.id] = leased.model_copy(
        update={"lease_expires_at": datetime(2026, 7, 16, tzinfo=UTC)}
    )

    interrupted = coordinator.audit_expired_leases(
        datetime(2026, 7, 17, tzinfo=UTC)
    )

    assert [item.id for item in interrupted] == [run.id]
    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.INTERRUPTED
    assert executor.phases == []


@pytest.mark.anyio
async def test_phase_failure_emits_safe_phase_and_run_failure_events() -> None:
    store, run, _executor, coordinator = _services()

    class FailingExecutor(RecordingPhaseExecutor):
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.DETERMINISTIC_RCA:
                raise RuntimeError("provider secret must not be persisted")
            return await super().execute_phase(phase_input)

    coordinator.phase_executor = FailingExecutor("inv-1")

    with pytest.raises(RuntimeError):
        await coordinator.execute(run.id, owner="worker-a")

    assert store.get_run(run.id).status == RuntimeRunStatus.FAILED
    assert store.get_run(run.id).failure_category.value == "unknown"
    assert store.list_attempts(run.id)[0].failure_category.value == "unknown"
    assert store.investigation_repository.get("inv-1").status.value == "failed"
    assert store.investigation_repository.get("inv-1").failure_reason
    assert [
        item.event_type.value for item in store.list_events(run.id)[-2:]
    ] == ["phase.failed", "run.failed"]
    assert "secret" not in str(
        [item.safe_payload for item in store.list_events(run.id)]
    ).lower()
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_escaping_contract_error_marks_run_contract_integrity() -> None:
    """逃逸出 phase 的模型输出契约违约应分类为 contract_integrity 而非 unknown。"""
    from backend.diagnosis.v11_runtime import V11RuntimeContractError

    store, run, _executor, coordinator = _services()

    class ContractFailingExecutor(RecordingPhaseExecutor):
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.DETERMINISTIC_RCA:
                raise V11RuntimeContractError("Investigator referenced uncommitted evidence")
            return await super().execute_phase(phase_input)

    coordinator.phase_executor = ContractFailingExecutor("inv-1")

    with pytest.raises(V11RuntimeContractError):
        await coordinator.execute(run.id, owner="worker-a")

    assert store.get_run(run.id).status == RuntimeRunStatus.FAILED
    assert (
        store.get_run(run.id).failure_category.value == "contract_integrity"
    )
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_duplicate_cancel_is_idempotent_and_emits_one_request_event() -> None:
    store, run, _executor, _coordinator = _services()
    executor = BlockingPhaseExecutor("inv-1")
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )
    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await executor.started.wait()

    await asyncio.gather(
        coordinator.request_cancel(run.id),
        coordinator.request_cancel(run.id),
    )
    executor.release.set()
    cancelled = await execution

    events = store.list_events(run.id)
    assert cancelled.status == RuntimeRunStatus.CANCELLED
    assert [item.event_type for item in events].count(
        RuntimeEventType.RUN_CANCEL_REQUESTED
    ) == 1
    assert store.investigation_repository.get("inv-1").status.value == "cancelled"
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_cancel_between_final_checkpoint_and_terminal_commit_wins() -> None:
    store, run, executor, _coordinator = _services()

    class PausingTerminalWriter(RuntimeWriter):
        def __init__(self, runtime_store) -> None:
            super().__init__(runtime_store)
            self.completion_started = asyncio.Event()
            self.release_completion = asyncio.Event()

        async def submit_terminal(self, commit):
            if commit.target_run_status == RuntimeRunStatus.COMPLETED:
                self.completion_started.set()
                await self.release_completion.wait()
            return await super().submit_terminal(commit)

    writer = PausingTerminalWriter(store)
    coordinator = RuntimeCoordinator(
        store=store,
        writer=writer,
        phase_executor=executor,
        heartbeat_seconds=1,
    )
    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await writer.completion_started.wait()
    await coordinator.request_cancel(run.id)
    writer.release_completion.set()

    cancelled = await execution

    assert cancelled.status == RuntimeRunStatus.CANCELLED
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.CANCELLED
    assert store.investigation_repository.get("inv-1").status.value == "cancelled"
    await coordinator.shutdown()


def test_escape_failure_category_maps_model_timeout_to_timeout():
    # 复审 H1：模型重试耗尽后从无 turn 级 catch 的 phase 逃逸的
    # ClassifiedRetryableError(TIMEOUT) 必须归一为 run 级 TIMEOUT，
    # 保持案例级有界重试的 phase 中立性；其他可重试类别仍保持 UNKNOWN。
    from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
    from backend.domain.multi_agent import FailureCategory
    from backend.domain.runtime import RuntimeFailureCategory
    from backend.runtime.coordinator import _escape_failure_category

    assert (
        _escape_failure_category(ClassifiedRetryableError(FailureCategory.TIMEOUT))
        is RuntimeFailureCategory.TIMEOUT
    )
    assert (
        _escape_failure_category(ClassifiedRetryableError(FailureCategory.TRANSPORT))
        is RuntimeFailureCategory.UNKNOWN
    )
    assert _escape_failure_category(ValueError("x")) is RuntimeFailureCategory.UNKNOWN
