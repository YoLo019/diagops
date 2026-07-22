import asyncio
import json
import time
from datetime import UTC, datetime
from threading import Barrier, Event, Lock

import pytest

from backend.db.schema import runtime_runs
from backend.diagnosis.adaptive_tools import (
    AdaptiveToolSession,
    tool_idempotency_key,
)
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.diff import RuntimeDiffService
from backend.runtime.phases import BusinessMutation, PhaseOutput, ToolCommit
from backend.runtime.store import RuntimeConflict, RuntimeLeaseLost
from backend.runtime.writer import RuntimeWriter
from backend.tools.registry import ToolInvocationResult, ToolRegistry


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="Users see 500s.",
        started_at=datetime(2026, 7, 15, 8, tzinfo=UTC),
        time_window_minutes=30,
    )


def _payload() -> str:
    return json.dumps(
        {
            "start_time": "2026-07-15T07:50:00+00:00",
            "end_time": "2026-07-15T08:10:00+00:00",
            "reason": "validate candidate",
        }
    )


def _registry(counter: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        lambda **kwargs: _success(counter, **kwargs),
    )
    return registry


def _success(counter: list[str], **kwargs) -> ToolInvocationResult:
    counter.append(kwargs["tool_name"])
    return ToolInvocationResult(
        call=ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
            input=kwargs["input"],
            status=ToolCallStatus.SUCCESS,
        ),
        evidence=[],
        provider_results=[],
    )


@pytest.mark.anyio
async def test_resume_reuses_committed_tool_result_without_invocation() -> None:
    invocations: list[str] = []
    query = {
        "start_time": "2026-07-15T07:50:00Z",
        "end_time": "2026-07-15T08:10:00Z",
        "limit": 20,
        "keywords": [],
        "levels": [],
        "instance": None,
    }
    key = tool_idempotency_key(
        run_id="run-1",
        agent_name=AgentName.LOG,
        logical_step="LogAgent:1:1",
        tool_name="read_logs",
        normalized_input=query,
    )
    committed = ToolCallRecord(
        id="tool-1",
        task_id="task-log",
        agent_name="log_agent",
        tool_name="read_logs",
        input={key: value for key, value in query.items() if value is not None},
        status=ToolCallStatus.SUCCESS,
        runtime_run_id="run-1",
        logical_call_id="LogAgent:1:1",
        idempotency_key=key,
        execution_id="exec-old",
    )
    persisted: list[ToolInvocationResult] = []
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=_registry(invocations),
        task_ids={AgentName.LOG: "task-log"},
        runtime_run_id="run-1",
        resolve_tool_result=lambda candidate: committed if candidate == key else None,
        persist_tool_result=lambda result: _persist(persisted, result),
        max_total_tool_calls=0,
    )

    response = json.loads(
        await session.invoke(AgentName.LOG, "read_logs", _payload(), 1)
    )

    assert response["status"] == "success"
    assert invocations == []
    assert persisted == []
    assert session.tool_calls == [committed]


@pytest.mark.anyio
async def test_late_tool_result_is_rejected_after_execution_fence_loss() -> None:
    invocations: list[str] = []
    persisted: list[ToolInvocationResult] = []
    checks = 0

    def check_execution() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RuntimeError("lease lost")

    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=_registry(invocations),
        task_ids={AgentName.LOG: "task-log"},
        runtime_run_id="run-1",
        persist_tool_result=lambda result: _persist(persisted, result),
        check_execution=check_execution,
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        await session.invoke(AgentName.LOG, "read_logs", _payload(), 1)

    assert invocations == ["read_logs"]
    assert persisted == []
    assert session.tool_calls == []


async def _persist(
    persisted: list[ToolInvocationResult], result: ToolInvocationResult
) -> ToolCallRecord:
    persisted.append(result)
    return result.call


@pytest.mark.anyio
async def test_each_model_boundary_has_a_new_execution_id() -> None:
    calls: list[str] = []
    events: list[tuple[str, str]] = []
    checks = 0

    async def turn(**kwargs):
        calls.append(kwargs["execution_id"])
        return object()

    def check_execution() -> None:
        nonlocal checks
        checks += 1

    async def persist_model_event(execution_id: str, status: str) -> None:
        events.append((execution_id, status))

    runtime = AgentsRcaRuntime(
        model="fake",
        turn=turn,
        check_execution=check_execution,
        persist_model_event=persist_model_event,
    )

    await runtime._invoke_turn(None, marker="first")
    await runtime._invoke_turn(None, marker="resume")

    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert all(item.startswith("model-exec-") for item in calls)
    assert checks == 4
    assert events == [
        (calls[0], "started"),
        (calls[0], "completed"),
        (calls[1], "started"),
        (calls[1], "completed"),
    ]


@pytest.mark.anyio
async def test_ambiguous_model_execution_keeps_started_audit_only() -> None:
    calls: list[str] = []
    events: list[tuple[str, str]] = []
    checks = 0

    async def turn(**kwargs):
        calls.append(kwargs["execution_id"])
        return object()

    def check_execution() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeLeaseLost("lease lost after model response")

    async def persist_model_event(execution_id: str, status: str) -> None:
        events.append((execution_id, status))

    runtime = AgentsRcaRuntime(
        model="fake",
        turn=turn,
        check_execution=check_execution,
        persist_model_event=persist_model_event,
    )

    with pytest.raises(RuntimeLeaseLost):
        await runtime._invoke_turn(None, marker="ambiguous")

    assert events == [(calls[0], "started")]


@pytest.mark.anyio
async def test_tool_io_is_bounded_to_three_parallel_steps_per_run() -> None:
    barrier = Barrier(3)
    lock = Lock()
    active = 0
    peak = 0

    def handler(**kwargs) -> ToolInvocationResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
            ),
            evidence=[],
            provider_results=[],
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        handler,
    )
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=registry,
        task_ids={AgentName.LOG: "task-log"},
        max_tool_calls_per_specialist=6,
        max_total_tool_calls=6,
        max_parallel_steps_per_run=3,
    )

    await asyncio.gather(
        *(
            session.invoke(
                AgentName.LOG,
                "read_logs",
                json.dumps(
                    {
                        **json.loads(_payload()),
                        "keywords": [f"keyword-{index}"],
                    }
                ),
                1,
            )
            for index in range(6)
        )
    )

    assert peak == 3


@pytest.mark.anyio
async def test_coordinator_resume_reuses_distinct_tools_committed_before_checkpoint(
    runtime_store,
) -> None:
    invocations: list[tuple[str, ...]] = []
    store = runtime_store
    repository = store.investigation_repository
    registry = ToolRegistry()

    def durable_success(**kwargs) -> ToolInvocationResult:
        keywords = tuple(kwargs["input"].get("keywords", []))
        invocations.append(keywords)
        evidence_suffix = keywords[0]
        evidence = EvidenceItem(
            id=f"ev-durable-tool-{evidence_suffix}",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 7, 15, 8, tzinfo=UTC),
            summary=f"durable tool evidence {evidence_suffix}",
        )
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
                output_evidence_ids=[evidence.id],
            ),
            evidence=[evidence],
            provider_results=[],
        )

    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        durable_success,
    )
    run = store.create_run(
        RuntimeRun(
            id="run-1",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )

    class DurableToolExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            state = phase_input.resume_state
            if phase_input.phase == RuntimePhase.INTAKE:
                session = AdaptiveToolSession(
                    event=_event(),
                    seed_evidence=[],
                    registry=registry,
                    task_ids={AgentName.LOG: f"task-{phase_input.attempt_id}"},
                    runtime_run_id=phase_input.run_id,
                    resolve_tool_result=phase_input.resolve_tool_result,
                    persist_tool_start=phase_input.persist_tool_start,
                    persist_tool_result=phase_input.persist_tool_result,
                    check_execution=phase_input.check_execution,
                )
                for keyword in ("checkout", "database"):
                    await session.invoke(
                        AgentName.LOG,
                        "read_logs",
                        json.dumps(
                            {
                                **json.loads(_payload()),
                                "keywords": [keyword],
                            }
                        ),
                        1,
                    )
                keys = [
                    call.idempotency_key
                    for call in repository.list_tool_calls("inv-1")
                    if call.status == ToolCallStatus.SUCCESS
                    and call.idempotency_key is not None
                ]
                state = RuntimeResumeState(
                    completed_evidence_ids=[
                        item.id for item in repository.get("inv-1").evidence
                    ],
                    successful_tool_keys=keys,
                )
                if phase_input.attempt_id == store.list_attempts(run.id)[0].id:
                    raise RuntimeLeaseLost("crash after durable tool commit")
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DurableToolExecutor(),
        heartbeat_seconds=1,
    )

    first = await coordinator.execute(run.id, owner="worker-a")
    assert first.status == RuntimeRunStatus.RUNNING
    expired = datetime(2026, 7, 16, tzinfo=UTC)
    if hasattr(store, "_runs"):
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"lease_expires_at": expired}
        )
    else:
        with store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(lease_expires_at=expired.isoformat())
            )
    coordinator.audit_expired_leases(datetime(2026, 7, 17, tzinfo=UTC))

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert invocations == [("checkout",), ("database",)]
    assert [item.id for item in repository.get("inv-1").evidence] == [
        "ev-durable-tool-checkout",
        "ev-durable-tool-database",
    ]
    successful_calls = [
        call
        for call in repository.list_tool_calls("inv-1")
        if call.status == ToolCallStatus.SUCCESS
    ]
    assert len(successful_calls) == 2
    assert len({call.logical_call_id for call in successful_calls}) == 2
    assert len({call.idempotency_key for call in successful_calls}) == 2
    assert [attempt.status for attempt in store.list_attempts(run.id)] == [
        RuntimeAttemptStatus.INTERRUPTED,
        RuntimeAttemptStatus.COMPLETED,
    ]
    assert store.list_checkpoints(run.id)[0].resume_state.successful_tool_keys
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "tool.started"
    ) == 2
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "tool.completed"
    ) == 2
    tool_events = [
        event
        for event in store.list_events(run.id)
        if event.event_type.value.startswith("tool.")
    ]
    assert tool_events
    assert all(event.safe_payload.get("normalized_inputs") for event in tool_events)
    projected = RuntimeDiffService(store=store).compare(run.id, run.id)
    assert projected.sections["tools"].left
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_coordinator_persists_model_execution_acceptance_events(
    runtime_store,
) -> None:
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id="run-model-audit",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )

    class ModelBoundaryExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.INTAKE:
                async def turn(**_kwargs):
                    return object()

                runtime = AgentsRcaRuntime(
                    model="fake",
                    turn=turn,
                    check_execution=phase_input.check_execution,
                    persist_model_event=phase_input.persist_model_event,
                )
                await runtime._invoke_turn(None, marker="coordinator")
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=ModelBoundaryExecutor(),
        heartbeat_seconds=1,
    )

    completed = await coordinator.execute(run.id, owner="worker-a")

    model_events = [
        event
        for event in store.list_events(run.id)
        if event.event_type.value.startswith("model.")
    ]
    assert completed.status == RuntimeRunStatus.COMPLETED
    assert [event.event_type.value for event in model_events] == [
        "model.started",
        "model.completed",
    ]
    assert model_events[0].execution_id == model_events[1].execution_id
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_running_cancel_discards_late_real_tool_result(runtime_store) -> None:
    store = runtime_store
    started = Event()
    release = Event()

    def handler(**kwargs) -> ToolInvocationResult:
        started.set()
        release.wait(timeout=2)
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
            ),
            evidence=[],
            provider_results=[],
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        handler,
    )
    run = store.create_run(
        RuntimeRun(
            id="run-tool-cancel",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )

    class ToolBoundaryExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.INTAKE:
                session = AdaptiveToolSession(
                    event=_event(),
                    seed_evidence=[],
                    registry=registry,
                    task_ids={AgentName.LOG: "task-cancel"},
                    runtime_run_id=phase_input.run_id,
                    resolve_tool_result=phase_input.resolve_tool_result,
                    persist_tool_start=phase_input.persist_tool_start,
                    persist_tool_result=phase_input.persist_tool_result,
                    check_execution=phase_input.check_execution,
                )
                await session.invoke(AgentName.LOG, "read_logs", _payload(), 1)
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=ToolBoundaryExecutor(),
        heartbeat_seconds=1,
    )
    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    while not started.is_set():
        await asyncio.sleep(0)
    await coordinator.request_cancel(run.id)
    release.set()

    cancelled = await execution

    calls = store.investigation_repository.list_tool_calls("inv-1")
    assert cancelled.status == RuntimeRunStatus.CANCELLED
    assert store.list_checkpoints(run.id) == []
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.INTERRUPTED
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "tool.completed"
    ) == 0
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_running_cancel_leaves_model_execution_ambiguous(runtime_store) -> None:
    store = runtime_store
    started = asyncio.Event()
    release = asyncio.Event()
    run = store.create_run(
        RuntimeRun(
            id="run-model-cancel",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )

    class ModelBoundaryExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.INTAKE:
                async def turn(**_kwargs):
                    started.set()
                    await release.wait()
                    return object()

                runtime = AgentsRcaRuntime(
                    model="fake",
                    turn=turn,
                    check_execution=phase_input.check_execution,
                    persist_model_event=phase_input.persist_model_event,
                )
                await runtime._invoke_turn(None, marker="cancel")
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=ModelBoundaryExecutor(),
        heartbeat_seconds=1,
    )
    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await started.wait()
    await coordinator.request_cancel(run.id)
    release.set()

    cancelled = await execution

    model_events = [
        event.event_type.value
        for event in store.list_events(run.id)
        if event.event_type.value.startswith("model.")
    ]
    assert cancelled.status == RuntimeRunStatus.CANCELLED
    assert model_events == ["model.started"]
    assert store.list_checkpoints(run.id) == []
    await coordinator.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_mode", ["timeout", "exception"])
async def test_tool_boundary_durably_replaces_running_with_same_failed_call(
    runtime_store, failure_mode: str
) -> None:
    store = runtime_store
    invocations = 0

    def handler(**kwargs) -> ToolInvocationResult:
        nonlocal invocations
        invocations += 1
        if failure_mode == "timeout":
            time.sleep(0.05)
        else:
            raise ValueError("tool adapter failed")
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
            ),
            evidence=[],
            provider_results=[],
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True), handler
    )
    run = store.create_run(
        RuntimeRun(
            id=f"run-tool-{failure_mode}",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )

    class ToolFailureExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.INTAKE:
                session = AdaptiveToolSession(
                    event=_event(),
                    seed_evidence=[],
                    registry=registry,
                    task_ids={AgentName.LOG: "task-failure"},
                    runtime_run_id=phase_input.run_id,
                    resolve_tool_result=phase_input.resolve_tool_result,
                    persist_tool_start=phase_input.persist_tool_start,
                    persist_tool_result=phase_input.persist_tool_result,
                    check_execution=phase_input.check_execution,
                    tool_timeout_seconds=0.005,
                )
                response = json.loads(
                    await session.invoke(
                        AgentName.LOG, "read_logs", _payload(), 1
                    )
                )
                assert response["status"] == "failed"
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=ToolFailureExecutor(),
        heartbeat_seconds=1,
    )

    completed = await coordinator.execute(run.id, owner="worker-a")
    await asyncio.sleep(0.06)

    calls = store.investigation_repository.list_tool_calls("inv-1")
    assert completed.status == RuntimeRunStatus.COMPLETED
    assert invocations == 1
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.FAILED
    assert calls[0].id.startswith("tool-")
    assert calls[0].logical_call_id.startswith("LogAgent:1:1:")
    assert calls[0].idempotency_key is not None
    assert calls[0].execution_id is not None
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "tool.started"
    ) == 1
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "tool.failed"
    ) == 1
    await coordinator.shutdown()


def test_late_tool_success_cannot_overwrite_durable_failure(runtime_store) -> None:
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id="run-late-tool-result",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
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
    running = ToolCallRecord(
        id="tool-stable",
        task_id="task-stable",
        agent_name=AgentName.LOG.value,
        tool_name="read_logs",
        status=ToolCallStatus.RUNNING,
        runtime_run_id=run.id,
        logical_call_id="LogAgent:1:1",
        idempotency_key="stable-key",
        execution_id="tool-exec-stable",
    )

    def commit(call: ToolCallRecord) -> None:
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

    commit(running)
    commit(
        running.model_copy(
            update={
                "status": ToolCallStatus.FAILED,
                "error_message": "timeout",
                "completed_at": datetime(2026, 7, 17, tzinfo=UTC),
            }
        )
    )
    with pytest.raises(RuntimeConflict, match="late result"):
        commit(running.model_copy(update={"status": ToolCallStatus.SUCCESS}))

    persisted = store.investigation_repository.list_tool_calls("inv-1")
    assert len(persisted) == 1
    assert persisted[0].status == ToolCallStatus.FAILED
