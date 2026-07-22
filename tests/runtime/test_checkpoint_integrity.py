
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.db.schema import runtime_checkpoints
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
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
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import (
    BusinessMutation,
    PhaseCommit,
    PhaseOutput,
    ToolCommit,
    checkpoint_digest,
)
from backend.runtime.store import RuntimeConflict, RuntimeIntegrityError
from backend.runtime.writer import RuntimeEventCommand, RuntimeWriter
from backend.services.incident_cases import load_incident_case

REFERENCE_FIELDS = (
    "completed_evidence_ids",
    "completed_finding_ids",
    "completed_review_ids",
    "completed_report_ids",
    "successful_tool_keys",
)


@pytest.mark.anyio
async def test_checkpoint_token_budget_matches_durable_model_usage(runtime_store) -> None:
    store = runtime_store
    run = store.create_run(
        RuntimeRun(
            id="run-token-checkpoint",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
            token_budget=10,
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
    store.append_event_command(
        RuntimeEventCommand(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            event_type=RuntimeEventType.MODEL_COMPLETED,
            actor_type=RuntimeActorType.MODEL,
            execution_id="model-exec-token",
            safe_payload={"status": "completed", "input_tokens": 4, "output_tokens": 2},
        )
    )
    commit = PhaseCommit(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=leased.lease_version,
        phase=RuntimePhase.INTAKE,
        business_mutation=BusinessMutation(investigation_id="inv-1"),
        safe_payload={"status": "completed"},
        resume_state=RuntimeResumeState(remaining_token_budget=5),
    )

    with pytest.raises(RuntimeIntegrityError, match="remaining token budget"):
        store.commit_phase(commit)

    checkpoint = store.commit_phase(
        replace(
            commit,
            checkpoint_id="checkpoint-token-correct",
            resume_state=RuntimeResumeState(remaining_token_budget=4),
        )
    )
    assert checkpoint.resume_state.remaining_token_budget == 4


def _orchestrator(repository) -> DiagnosisOrchestrator:
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )


def _tampered_state(
    state: RuntimeResumeState, mutation: tuple[str, str]
) -> RuntimeResumeState:
    field, operation = mutation
    values = state.model_dump(mode="python")
    if field in REFERENCE_FIELDS:
        current = list(values[field])
        if operation == "add":
            current.append(f"missing-{field}")
        elif operation == "delete":
            current = current[1:]
        else:
            current[0] = f"replacement-{field}"
        values[field] = current
    else:
        delta = 1 if operation == "increment" else -1
        values[field] = values[field] + delta
    return RuntimeResumeState.model_validate(values)


TAMPERS = tuple(
    (field, operation)
    for field in REFERENCE_FIELDS
    for operation in ("add", "delete", "replace")
) + (
    ("remaining_tool_budget", "increment"),
    ("remaining_tool_budget", "decrement"),
    ("remaining_token_budget", "increment"),
    ("remaining_token_budget", "decrement"),
)


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", TAMPERS)
async def test_resume_rejects_exact_projection_and_budget_tamper(
    runtime_store, tamper: tuple[str, str]
) -> None:
    store = runtime_store
    repository = store.investigation_repository
    orchestrator = _orchestrator(repository)
    result = await DiagnosisPhaseExecutor(orchestrator).execute(
        load_incident_case("deployment_regression"),
        strategy=InvestigationStrategy.FIXED,
    )
    record = result.record
    tool = ToolCallRecord(
        id="tool-checkpoint",
        task_id="task-checkpoint",
        agent_name="LogAgent",
        tool_name="read_logs",
        status=ToolCallStatus.SUCCESS,
        runtime_run_id="run-checkpoint",
        logical_call_id="LogAgent:1:1",
        idempotency_key="checkpoint-tool-key",
        execution_id="tool-exec-checkpoint",
    )
    repository.save_tool_calls(record.id, [tool])
    run = store.create_run(
        RuntimeRun(
            id="run-checkpoint",
            investigation_id=record.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
            tool_budget=2,
            token_budget=10,
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
    findings = repository.list_agent_findings(record.id)
    review = repository.get_coordination_review(record.id)
    state = RuntimeResumeState(
        completed_evidence_ids=[item.id for item in record.evidence],
        completed_finding_ids=[item.id for item in findings],
        completed_review_ids=[review.id] if review is not None else [],
        completed_report_ids=[record.report.id] if record.report is not None else [],
        successful_tool_keys=["checkpoint-tool-key"],
        remaining_tool_budget=1,
        remaining_token_budget=10,
    )
    checkpoint = store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id=record.id),
            safe_payload={"status": "completed"},
            resume_state=state,
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    tampered = _tampered_state(checkpoint.resume_state, tamper)
    updated = checkpoint.model_copy(
        update={
            "resume_state": tampered,
            "state_digest": checkpoint_digest(
                run_id=checkpoint.run_id,
                attempt_id=checkpoint.attempt_id,
                completed_phase=checkpoint.completed_phase,
                resume_state=tampered,
            ),
        }
    )
    if hasattr(store, "_checkpoints"):
        store._checkpoints[checkpoint.id] = updated
    else:
        with store.engine.begin() as connection:
            connection.execute(
                runtime_checkpoints.update()
                .where(runtime_checkpoints.c.id == checkpoint.id)
                .values(
                    resume_state=tampered.model_dump(mode="json"),
                    state_digest=updated.state_digest,
                )
            )

    class RejectExternalExecution:
        calls = 0

        async def execute_phase(self, _phase_input):
            self.calls += 1
            raise AssertionError("invalid recovery must not execute a phase")

    executor = RejectExternalExecution()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
    )
    with pytest.raises(RuntimeConflict, match="checkpoint"):
        await coordinator.resume(run.id, owner="worker-b")

    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    assert len(store.list_attempts(run.id)) == 1
    assert executor.calls == 0
    assert store.list_events(run.id)[-1].event_type.value == "recovery.rejected"
    await coordinator.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("entity", ("evidence", "finding", "tool"))
async def test_resume_rejects_unproven_post_checkpoint_projection_addition(
    runtime_store,
    entity: str,
) -> None:
    store = runtime_store
    repository = store.investigation_repository
    result = await DiagnosisPhaseExecutor(_orchestrator(repository)).execute(
        load_incident_case("deployment_regression"),
        strategy=InvestigationStrategy.FIXED,
    )
    record = result.record
    findings = repository.list_agent_findings(record.id)
    review = repository.get_coordination_review(record.id)
    run = store.create_run(
        RuntimeRun(
            id=f"run-unproven-{entity}",
            investigation_id=record.id,
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
    state = RuntimeResumeState(
        completed_evidence_ids=[item.id for item in record.evidence],
        completed_finding_ids=[item.id for item in findings],
        completed_review_ids=[review.id] if review is not None else [],
        completed_report_ids=[record.report.id] if record.report is not None else [],
    )
    store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id=record.id),
            safe_payload={"status": "completed"},
            resume_state=state,
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    if entity == "evidence":
        record.evidence.append(
            record.evidence[0].model_copy(update={"id": "unproven-evidence"})
        )
        repository.save(record)
    elif entity == "finding":
        repository.save_agent_findings(
            record.id,
            [findings[0].model_copy(update={"id": "unproven-finding"})],
        )
    else:
        repository.save_tool_calls(
            record.id,
            [
                ToolCallRecord(
                    id="unproven-tool",
                    task_id="task-unproven",
                    agent_name="LogAgent",
                    tool_name="read_logs",
                    status=ToolCallStatus.SUCCESS,
                    runtime_run_id=run.id,
                    logical_call_id="LogAgent:unproven",
                    idempotency_key="unproven-key",
                    execution_id="tool-exec-unproven",
                )
            ],
        )

    class RejectExternalExecution:
        calls = 0

        async def execute_phase(self, _phase_input):
            self.calls += 1
            raise AssertionError("unproven projection must not execute a phase")

    executor = RejectExternalExecution()
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
    )

    with pytest.raises(RuntimeConflict, match="checkpoint"):
        await coordinator.resume(run.id, owner="worker-b")

    assert len(store.list_attempts(run.id)) == 1
    assert executor.calls == 0
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_resume_accepts_tool_proven_post_checkpoint_evidence(runtime_store) -> None:
    store = runtime_store
    repository = store.investigation_repository
    run = store.create_run(
        RuntimeRun(
            id="run-proven-tool-evidence",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            tool_budget=2,
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
            resume_state=RuntimeResumeState(remaining_tool_budget=2),
        )
    )
    evidence = EvidenceItem(
        id="proven-evidence",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 17, tzinfo=UTC),
        summary="tool-proven evidence",
    )
    call = ToolCallRecord(
        id="proven-tool",
        task_id="task-proven",
        agent_name="LogAgent",
        tool_name="read_logs",
        status=ToolCallStatus.SUCCESS,
        output_evidence_ids=[evidence.id],
        runtime_run_id=run.id,
        logical_call_id="LogAgent:proven",
        idempotency_key="proven-key",
        execution_id="tool-exec-proven",
    )
    record = repository.get("inv-1")
    record.evidence = [evidence]
    store.commit_tool(
        ToolCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=leased.lease_version,
            business_mutation=BusinessMutation(
                investigation_id="inv-1",
                investigation=record,
                tool_calls=(call,),
            ),
            call=call,
        )
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )

    class ProjectionExecutor:
        async def execute_phase(self, phase_input):
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state.model_copy(
                    update={
                        "completed_evidence_ids": [evidence.id],
                        "successful_tool_keys": ["proven-key"],
                        "remaining_tool_budget": phase_input.tool_budget,
                    }
                ),
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=ProjectionExecutor(),
    )

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status == RuntimeRunStatus.COMPLETED
    assert store.list_checkpoints(run.id)[-1].resume_state.remaining_tool_budget == 1
    await coordinator.shutdown()
