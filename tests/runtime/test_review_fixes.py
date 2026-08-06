from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from backend.config.settings import AppSettings, RuntimeSettings, StorageSettings
from backend.db.models import InvestigationRecord
from backend.db.schema import runtime_runs
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    DiagnosisTask,
    DiagnosisTaskType,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import (
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionContractVersion,
    ExecutionStepKind,
    LeadAction,
    ModelProvider,
)
from backend.domain.runtime import (
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.runtime.diff import RuntimeDiffService, reseal_frozen_projection
from backend.runtime.phases import BusinessMutation, ToolCommit
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeContractError,
    RuntimeIntegrityError,
)
from backend.services.container import AppContainer
from tests.runtime.test_replay import _services, _valid_events
from tests.runtime.test_v11_isolation_red import _v11_run


def _v11_source(source):
    return source.model_copy(
        update={
            "execution_contract_version": ExecutionContractVersion.V11,
            "authority_mode": AuthorityMode.AGENT,
            "model_provider": ModelProvider.OPENAI,
            "model_name": "gpt-test",
            "prompt_version": "v11-test",
            "tool_budget": 8,
            "token_budget": 1000,
            "timeout_seconds": 120.0,
            "execution_contract": {
                "tool_budget": 8,
                "token_budget": 1000,
                "timeout_seconds": 120.0,
                "execution_contract_version": "v11",
                "authority_mode": "agent",
                "model_provider": "openai",
                "model_name": "gpt-test",
                "prompt_version": "v11-test",
            },
        }
    )


def test_replay_rejects_v11_phases_out_of_contract_order() -> None:
    store, _repository, source = _services()
    source = _v11_source(source)
    events = _valid_events(source.id)
    events[2] = events[2].model_copy(update={"phase": RuntimePhase.EVIDENCE_COLLECTION})
    events[3] = events[3].model_copy(update={"phase": RuntimePhase.EVIDENCE_COLLECTION})
    events[4:4] = [
        events[2].model_copy(
            update={
                "id": "event-intake-start",
                "phase": RuntimePhase.INTAKE,
                "safe_payload": {"status": "running"},
            }
        ),
        events[3].model_copy(
            update={
                "id": "event-intake-complete",
                "phase": RuntimePhase.INTAKE,
                "safe_payload": {"status": "completed"},
            }
        ),
    ]
    for sequence, event in enumerate(events, start=1):
        event.sequence = sequence

    errors = ReplayService(ReplayDependencies(store=store))._validate_events(
        source, events, None
    )

    assert "illegal_phase_order" in errors


def test_replay_rejects_resealed_frozen_projection_with_foreign_runtime_owner() -> None:
    store, _repository, source = _services()
    run = store.create_run(
        _v11_source(source).model_copy(
            update={
                "id": "run-v11-frozen-owner",
                "status": RuntimeRunStatus.COMPLETED,
            }
        )
    )
    frozen = store.get_frozen_business_projection(run.id)
    store._frozen_business_projections[run.id] = reseal_frozen_projection(
        {**frozen, "runtime_run_id": "foreign-runtime"}
    )

    report = ReplayService(ReplayDependencies(store=store)).replay(run.id)

    assert report.valid is False
    assert "frozen_projection_source_mismatch" in report.validation_errors


def test_diff_rejects_resealed_frozen_projection_with_foreign_runtime_owner() -> None:
    store, _repository, source = _services()
    run = store.create_run(
        _v11_source(source).model_copy(
            update={
                "id": "run-v11-diff-owner",
                "status": RuntimeRunStatus.COMPLETED,
            }
        )
    )
    frozen = store.get_frozen_business_projection(run.id)
    store._frozen_business_projections[run.id] = reseal_frozen_projection(
        {**frozen, "runtime_run_id": "foreign-runtime"}
    )

    result = RuntimeDiffService(store=store).compare(run.id, run.id)

    assert result.sections["causes"].left == {"status": "unavailable"}


def test_v11_tool_commit_rejects_foreign_business_mutation_owner(runtime_store) -> None:
    run = runtime_store.create_run(_v11_run(run_id="run-tool-owner"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-tool",
        expected_status=RuntimeRunStatus.CREATED,
    )
    repository = runtime_store.investigation_repository
    record = repository.save(
        repository.get("inv-1").model_copy(update={"active_runtime_run_id": run.id})
    )
    call = ToolCallRecord(
        id="tool-owner",
        task_id="task-owner",
        agent_name="LogAgent",
        tool_name="read_logs",
        status=ToolCallStatus.SUCCESS,
        runtime_run_id=run.id,
        logical_call_id="LogAgent:1:1",
        idempotency_key="tool-owner-key",
        execution_id="tool-exec-owner",
    )

    with pytest.raises(RuntimeIntegrityError, match="owner"):
        runtime_store.commit_tool(
            ToolCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-tool",
                lease_version=leased.lease_version,
                business_mutation=BusinessMutation(
                    investigation_id=record.id,
                    investigation=record.model_copy(
                        update={"active_runtime_run_id": "foreign-runtime"}
                    ),
                    tool_calls=(call,),
                ),
                call=call,
            )
        )


def test_sqlite_contract_integrity_reload_terminates_running_attempt_and_is_idempotent(
    runtime_store,
) -> None:
    if not hasattr(runtime_store, "engine"):
        pytest.skip("contract repair requires SQLite durable event storage")
    run = runtime_store.create_run(_v11_run(run_id="run-contract-running"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-contract",
        expected_status=RuntimeRunStatus.CREATED,
    )
    with runtime_store.engine.begin() as connection:
        connection.execute(
            update(runtime_runs)
            .where(runtime_runs.c.id == run.id)
            .values(execution_contract={**run.execution_contract, "model_name": "tampered"})
        )

    repaired = runtime_store.get_run(run.id)
    events = runtime_store.list_events(run.id)
    assert repaired.status == RuntimeRunStatus.FAILED
    assert repaired.failure_category == RuntimeFailureCategory.CONTRACT_INTEGRITY
    assert runtime_store.get_attempt(attempt.id).status == RuntimeAttemptStatus.FAILED
    assert repaired.lease_owner is None
    assert any(
        event.event_type == RuntimeEventType.RUN_FAILED
        and event.safe_payload.get("failure_category") == "contract_integrity"
        for event in events
    )
    event_ids = [event.id for event in events]

    runtime_store.get_run(run.id)

    assert [event.id for event in runtime_store.list_events(run.id)] == event_ids


def _container_record(investigation_id: str) -> InvestigationRecord:
    return InvestigationRecord(
        id=investigation_id,
        event=IncidentEvent(
            source=IncidentSource.MANUAL,
            service="review-fix",
            environment="test",
            severity=Severity.WARNING,
            title="review fix",
            description="review fix",
            started_at=datetime(2026, 8, 6, tzinfo=UTC),
        ),
    )


def test_create_v11_runtime_run_rejects_disabled_runtime_before_linking() -> None:
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            runtime=RuntimeSettings(enabled=False),
        )
    )
    container.repository.save(_container_record("inv-disabled"))

    with pytest.raises(RuntimeConflict, match="enabled Runtime"):
        container.create_runtime_run(
            "inv-disabled",
            strategy="adaptive",
            run_reason="initial",
            execution_contract_version=ExecutionContractVersion.V11,
        )

    assert [item.id for item in container.repository.list()] == ["inv-disabled"]
    container.close()


def test_create_v11_runtime_run_rejects_explicit_server_tuple_mismatch() -> None:
    container = AppContainer(AppSettings(storage=StorageSettings(url="memory://")))
    container.repository.save(_container_record("inv-tuple"))

    with pytest.raises(RuntimeContractError, match="server-owned"):
        container.create_runtime_run(
            "inv-tuple",
            strategy="adaptive",
            run_reason="initial",
            model_name="caller-selected-model",
            execution_contract_version=ExecutionContractVersion.V11,
        )

    assert [item.id for item in container.repository.list()] == ["inv-tuple"]
    container.close()


def test_v11_summary_uses_coordination_review_authority_projection(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    record = repository.save(
        repository.get("inv-1").model_copy(
            update={"active_runtime_run_id": "run-summary"}
        )
    )
    candidate = RootCauseCandidate(
        id="candidate-summary",
        summary="review candidate",
        rank=1,
        confidence=0.6,
        affected_entity="checkout-api",
        failure_mechanism="configuration drift",
    )
    assessment = CriticAssessment(
        id="assessment-summary",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="bounded unknown",
                gap="needs evidence",
            )
            for name in CausalCheckName
        ],
        summary="accepted with bounded uncertainty",
        runtime_run_id="run-summary",
    )
    review = CoordinationReview(
        investigation_id=record.id,
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision={
            "action": LeadAction.CONCLUDE,
            "summary": "conclude from accepted candidate",
            "candidate_ids": [candidate.id],
        },
        diagnostic_status=DiagnosticStatus.COMPLETE,
        runtime_run_id="run-summary",
        authority_mode=AuthorityMode.AGENT,
        summary="review projection",
    )
    repository.save_coordination_review(review)

    summary = next(
        item for item in repository.list_summaries() if item.id == record.id
    )

    assert summary.top_cause_type == "unknown"
    assert summary.top_affected_entity == "checkout-api"
    assert summary.top_failure_mechanism == "configuration drift"
    assert summary.diagnostic_status == DiagnosticStatus.COMPLETE
    assert summary.authority_mode == AuthorityMode.AGENT
    assert summary.lead_decision.candidate_ids == [candidate.id]
    assert summary.critic_assessments[0].candidate_id == candidate.id


def test_v11_coordination_review_requires_critic_assessment_owner(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    candidate = RootCauseCandidate(
        id="candidate-owner-required",
        summary="owner required",
        rank=1,
        confidence=0.6,
    )
    assessment = CriticAssessment(
        id="assessment-owner-required",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="bounded unknown",
                gap="needs evidence",
            )
            for name in CausalCheckName
        ],
        summary="missing owner",
        runtime_run_id="run-review-owner",
    )
    valid_review = CoordinationReview(
        investigation_id="inv-1",
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision={
            "action": LeadAction.CONCLUDE,
            "summary": "reject unowned assessment",
            "candidate_ids": [candidate.id],
        },
        runtime_run_id="run-review-owner",
        authority_mode=AuthorityMode.AGENT,
    )
    review = valid_review.model_copy(
        update={
            "critic_assessments": [
                assessment.model_copy(update={"runtime_run_id": None})
            ]
        }
    )

    with pytest.raises(ValueError, match="owner"):
        repository.save_coordination_review(review)


@pytest.mark.parametrize("invalid_linkage", ["round", "assessment"])
def test_v11_round_two_investigator_finding_requires_linked_round_two_task(
    runtime_store, invalid_linkage: str
) -> None:
    repository = runtime_store.investigation_repository
    run_id = "run-finding-linkage"
    candidate = RootCauseCandidate(
        id="candidate-finding-linkage",
        summary="finding linkage",
        rank=1,
        confidence=0.5,
    )
    task_id = "task-finding-linkage"
    assessment = CriticAssessment(
        id="assessment-finding-linkage",
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="bounded unknown",
                gap="needs evidence",
            )
            for name in CausalCheckName
        ],
        gap="request the linked task",
        supplemental_task_ids=(
            ["other-task"] if invalid_linkage == "assessment" else [task_id]
        ),
        summary="request more evidence",
        runtime_run_id=run_id,
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision={
            "action": LeadAction.INVESTIGATE,
            "summary": "run the requested task",
            "task_ids": [task_id],
        },
        runtime_run_id=run_id,
        authority_mode=AuthorityMode.AGENT,
    )
    repository.save_coordination_review(review)
    task = DiagnosisTask(
        id=task_id,
        title="linked task",
        description="linked task",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name=FindingActor.INVESTIGATOR,
        analysis_round=1 if invalid_linkage == "round" else 2,
        runtime_run_id=run_id,
        critic_assessment_id=(
            assessment.id if invalid_linkage != "round" else None
        ),
        information_gap="request the linked task",
    )
    repository.save_tasks("inv-1", [task])
    finding = AgentFinding(
        id="finding-linkage",
        investigation_id="inv-1",
        agent_name=FindingActor.INVESTIGATOR,
        agent_instance_id="investigator-1",
        finding_type=AgentFindingType.GAP,
        summary="round two finding",
        confidence=0.5,
        analysis_round=2,
        task_id=task_id,
        runtime_run_id=run_id,
        critic_assessment_id=assessment.id,
    )

    with pytest.raises(ValueError, match="(round|assessment|task)"):
        repository.save_agent_findings("inv-1", [finding])


def test_v11_result_validation_execution_requires_runtime_owner() -> None:
    with pytest.raises(ValueError, match="runtime_run_id"):
        AgentExecution(
            task_id="task-result-validation",
            agent_name="CoordinatorAgent",
            analysis_round=2,
            step_kind=ExecutionStepKind.RESULT_VALIDATION,
        )


def test_v11_result_validation_execution_persistence_rejects_missing_owner(
    runtime_store,
) -> None:
    execution = AgentExecution(
        id="execution-result-validation",
        task_id="task-result-validation",
        agent_name="CoordinatorAgent",
        analysis_round=2,
        step_kind=ExecutionStepKind.RESULT_VALIDATION,
        runtime_run_id="run-result-validation",
    ).model_copy(update={"runtime_run_id": None})

    with pytest.raises(ValueError, match="runtime_run_id"):
        runtime_store.investigation_repository.save_executions(
            "inv-1", [execution]
        )
