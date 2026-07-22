import json
from datetime import UTC, datetime

import pytest

from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.domain.agent_findings import AgentName
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
from backend.runtime.faults import (
    APPROVED_FAULT_POINTS,
    CONNECTED_M3_FAULT_POINTS,
    DEFERRED_M3_FAULT_POINTS,
    DeterministicFaultInjector,
    NoFaultInjector,
    RuntimeInjectedFault,
)
from backend.runtime.phases import BusinessMutation, PhaseCommit, PhaseOutput
from backend.runtime.store import RuntimePersistenceError
from backend.runtime.writer import RuntimeWriter
from backend.tools.registry import ToolInvocationResult, ToolRegistry


def test_no_fault_injector_never_raises_for_approved_points() -> None:
    assert len(APPROVED_FAULT_POINTS) == 14
    injector = NoFaultInjector()
    for point in APPROVED_FAULT_POINTS:
        injector.hit(point)


def test_deterministic_fault_injector_raises_only_at_configured_hit() -> None:
    point = "before_phase_start"
    injector = DeterministicFaultInjector({point: 2})

    injector.hit(point)
    with pytest.raises(RuntimeInjectedFault, match=point):
        injector.hit(point)
    injector.hit("lease_lost")


def test_every_approved_fault_is_triggered_by_the_deterministic_injector() -> None:
    for point in APPROVED_FAULT_POINTS:
        injector = DeterministicFaultInjector({point: 1})
        with pytest.raises(RuntimeInjectedFault, match=point):
            injector.hit(point)


def test_phase_persistence_fault_rolls_back_business_and_runtime_projection(
    runtime_store,
) -> None:
    store = runtime_store
    original = store.investigation_repository.get("inv-1")
    run = store.create_run(
        RuntimeRun(
            id="run-phase-transaction-fault",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
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
    store.fault_injector = DeterministicFaultInjector(
        {"persistence_mid_transaction": 1}
    )

    with pytest.raises(RuntimePersistenceError):
        store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=leased.lease_version,
                phase=RuntimePhase.INTAKE,
                business_mutation=BusinessMutation(
                    investigation_id="inv-1",
                    investigation=original.model_copy(
                        update={"failure_reason": "must roll back"}
                    ),
                ),
                safe_payload={"status": "completed"},
                resume_state=RuntimeResumeState(),
            )
        )

    assert store.investigation_repository.get("inv-1") == original
    assert store.list_events(run.id) == []
    assert store.list_checkpoints(run.id) == []
    persisted_run = store.get_run(run.id)
    assert persisted_run.current_phase is None
    assert persisted_run.latest_checkpoint_id is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "point",
    [
        "before_phase_start",
        "provider_before_commit",
        "tool_after_commit_before_checkpoint",
        "model_after_send",
        "persistence_mid_transaction",
        "lease_lost",
        "unsafe_event_payload",
    ],
)
async def test_injected_boundary_crash_leaves_run_for_lease_recovery(
    runtime_store, point: str
) -> None:
    store = runtime_store
    record = store.investigation_repository.get("inv-1")
    run = store.create_run(
        RuntimeRun(
            id=f"run-fault-{point}",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        lambda **kwargs: ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
            ),
            evidence=[],
            provider_results=[],
        ),
    )

    class BoundaryExecutor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            if phase_input.phase == RuntimePhase.INTAKE:
                if point == "provider_before_commit":
                    phase_input.hit_fault("provider_before_commit")
                elif point == "tool_after_commit_before_checkpoint":
                    session = AdaptiveToolSession(
                        event=record.event,
                        seed_evidence=[],
                        registry=registry,
                        task_ids={AgentName.LOG: "task-fault"},
                        runtime_run_id=phase_input.run_id,
                        resolve_tool_result=phase_input.resolve_tool_result,
                        persist_tool_start=phase_input.persist_tool_start,
                        persist_tool_result=phase_input.persist_tool_result,
                        check_execution=phase_input.check_execution,
                    )
                    await session.invoke(
                        AgentName.LOG,
                        "read_logs",
                        json.dumps(
                            {
                                "start_time": "2026-07-17T00:00:00Z",
                                "end_time": "2026-07-17T00:10:00Z",
                                "reason": "fault boundary",
                            }
                        ),
                        1,
                    )
                elif point == "model_after_send":
                    async def turn(**_kwargs):
                        return object()

                    runtime = AgentsRcaRuntime(
                        model="fake",
                        turn=turn,
                        check_execution=phase_input.check_execution,
                        persist_model_event=phase_input.persist_model_event,
                    )
                    runtime._hit_fault = phase_input.hit_fault
                    await runtime._invoke_turn(None, marker="fault")
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
        fault_injector=DeterministicFaultInjector({point: 1}),
    )

    crashed = await coordinator.execute(run.id, owner="worker-a")

    assert crashed.status == RuntimeRunStatus.RUNNING
    if hasattr(store, "_runs"):
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"lease_expires_at": datetime(2026, 7, 16, tzinfo=UTC)}
        )
    else:
        from backend.db.schema import runtime_runs

        with store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(lease_expires_at="2026-07-16T00:00:00+00:00")
            )
    assert coordinator.audit_expired_leases(
        datetime(2026, 7, 17, tzinfo=UTC)
    )[0].status == RuntimeRunStatus.INTERRUPTED
    await coordinator.shutdown()


def test_m3_fault_points_track_connected_and_still_deferred_seams() -> None:
    assert DEFERRED_M3_FAULT_POINTS == {
        "otel_unavailable",
        "replay_external_call",
        "sse_reconnect",
    }
    assert CONNECTED_M3_FAULT_POINTS == DEFERRED_M3_FAULT_POINTS
    assert CONNECTED_M3_FAULT_POINTS <= APPROVED_FAULT_POINTS
    DeterministicFaultInjector({"otel_unavailable": 1})
    DeterministicFaultInjector({"replay_external_call": 1})


@pytest.mark.anyio
@pytest.mark.parametrize("point", ["checkpoint_tamper", "concurrent_resume"])
async def test_resume_fault_points_run_before_recovery_mutation(
    runtime_store, point: str
) -> None:
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id=f"run-resume-{point}",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
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

    class Executor:
        async def execute_phase(self, _phase_input):
            raise AssertionError("fault must happen before external execution")

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=Executor(),
        fault_injector=DeterministicFaultInjector({point: 1}),
    )
    with pytest.raises(RuntimeInjectedFault, match=point):
        await coordinator.resume(run.id, owner="worker-b")

    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    assert len(store.list_attempts(run.id)) == 1
    await coordinator.shutdown()
