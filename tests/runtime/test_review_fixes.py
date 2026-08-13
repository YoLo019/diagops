from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from backend.config.settings import AppSettings, RuntimeSettings, StorageSettings
from backend.db.models import InvestigationRecord
from backend.db.schema import runtime_runs
from backend.domain.actions import ActionRiskLevel, ActionType, RecommendedAction
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
    DiagnosisPlan,
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
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
)
from backend.domain.runtime import (
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.runtime.diff import RuntimeDiffService, reseal_frozen_projection
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import (
    BusinessMutation,
    PhaseCommit,
    PhaseInput,
    ToolCommit,
)
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


def test_v11_tool_budget_reservation_is_atomic_and_durable(runtime_store) -> None:
    contract = {
        **_v11_run().execution_contract,
        "tool_budget": 1,
    }
    run = runtime_store.create_run(
        _v11_run(
            run_id="run-tool-budget-reservation",
            tool_budget=1,
            execution_contract=contract,
        )
    )
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-tool-budget",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.investigation_repository.activate_projection("inv-1", run.id)

    def commit(call_id: str) -> None:
        call = ToolCallRecord(
            id=call_id,
            task_id=f"task-{call_id}",
            agent_name="InvestigatorAgent",
            tool_name="read_logs",
            status=ToolCallStatus.RUNNING,
            runtime_run_id=run.id,
            logical_call_id=call_id,
            idempotency_key=call_id,
            execution_id=f"execution-{call_id}",
        )
        runtime_store.commit_tool(
            ToolCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-tool-budget",
                lease_version=leased.lease_version,
                business_mutation=BusinessMutation(
                    investigation_id="inv-1",
                    tool_calls=(call,),
                ),
                call=call,
                phase=RuntimePhase.INVESTIGATOR_ROUND_1,
            )
        )

    commit("tool-reservation-1")
    with pytest.raises(RuntimeConflict, match="budget"):
        commit("tool-reservation-2")


@pytest.mark.parametrize(
    ("tool_name", "normalized_input"),
    [
        (
            "query_related_alerts",
            {
                "window_start": "2026-07-15T07:50:00+00:00",
                "window_end": "2026-07-15T08:10:00+00:00",
                "limit": 50,
                "severities": ["critical"],
                "statuses": ["firing", "resolved"],
                "entity_ids": ["checkout-service"],
            },
        ),
        (
            "query_traces",
            {
                "window_start": "2026-07-15T07:50:00+00:00",
                "window_end": "2026-07-15T08:10:00+00:00",
                "limit": 50,
                "service": "checkout-service",
                "error_only": False,
                "min_duration_ms": 12.5,
                "direction": "both",
            },
        ),
        (
            "read_runtime_state",
            {"limit": 50, "states": ["crash_loop"], "include_healthy": False},
        ),
        (
            "lookup_memory",
            {
                "affected_entity": "checkout-service",
                "failure_mechanism": "database connection pool exhausted",
                "limit": 5,
            },
        ),
    ],
)
def test_v11_scoped_tool_commits_persist_normalized_inputs(
    runtime_store, tool_name, normalized_input
) -> None:
    run = runtime_store.create_run(_v11_run(run_id=f"run-scoped-{tool_name}"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-scoped-tool",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.investigation_repository.activate_projection("inv-1", run.id)

    call = ToolCallRecord(
        id=f"call-{tool_name}",
        task_id=f"task-{tool_name}",
        agent_name="InvestigatorAgent",
        tool_name=tool_name,
        input=normalized_input,
        status=ToolCallStatus.SUCCESS,
        runtime_run_id=run.id,
        logical_call_id=f"logical-{tool_name}",
        idempotency_key=f"idem-{tool_name}",
        execution_id=f"execution-{tool_name}",
    )
    runtime_store.commit_tool(
        ToolCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-scoped-tool",
            lease_version=leased.lease_version,
            business_mutation=BusinessMutation(
                investigation_id="inv-1",
                tool_calls=(call,),
            ),
            call=call,
            phase=RuntimePhase.INVESTIGATOR_ROUND_1,
        )
    )

    events = [
        event
        for event in runtime_store.list_events(run.id)
        if event.event_type == RuntimeEventType.TOOL_COMPLETED
    ]
    assert len(events) == 1
    assert events[0].safe_payload["tool_name"] == tool_name
    assert events[0].safe_payload["normalized_inputs"] == normalized_input


def test_v11_transport_retry_reuses_one_durable_tool_reservation(runtime_store) -> None:
    contract = {
        **_v11_run().execution_contract,
        "tool_budget": 1,
    }
    run = runtime_store.create_run(
        _v11_run(
            run_id="run-tool-retry-reservation",
            tool_budget=1,
            execution_contract=contract,
        )
    )
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-tool-retry",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.investigation_repository.activate_projection("inv-1", run.id)

    def commit(
        call_id: str,
        logical_call_id: str,
        status: ToolCallStatus,
    ) -> None:
        call = ToolCallRecord(
            id=call_id,
            task_id=f"task-{call_id}",
            agent_name="InvestigatorAgent",
            tool_name="read_logs",
            status=status,
            runtime_run_id=run.id,
            logical_call_id=logical_call_id,
            idempotency_key=call_id,
            execution_id=f"execution-{call_id}",
        )
        runtime_store.commit_tool(
            ToolCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-tool-retry",
                lease_version=leased.lease_version,
                business_mutation=BusinessMutation(
                    investigation_id="inv-1",
                    tool_calls=(call,),
                ),
                call=call,
                phase=RuntimePhase.INVESTIGATOR_ROUND_1,
            )
        )

    commit("tool-retry-1", "logical-retry", ToolCallStatus.RUNNING)
    commit("tool-retry-1", "logical-retry", ToolCallStatus.FAILED)
    commit("tool-retry-2", "logical-retry", ToolCallStatus.RUNNING)

    with pytest.raises(RuntimeConflict, match="budget"):
        commit("tool-new", "logical-new", ToolCallStatus.RUNNING)

    calls = runtime_store.investigation_repository.list_tool_calls("inv-1")
    assert [call.id for call in calls] == ["tool-retry-1", "tool-retry-2"]
    assert calls[0].status == ToolCallStatus.FAILED
    assert calls[1].status == ToolCallStatus.RUNNING
    assert {call.logical_call_id for call in calls} == {"logical-retry"}


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


def test_sqlite_contract_integrity_repair_persists_repaired_contract_once(
    runtime_store,
) -> None:
    if not hasattr(runtime_store, "engine"):
        pytest.skip("contract repair requires SQLite durable event storage")
    run = runtime_store.create_run(_v11_run(run_id="run-contract-persist"))
    with runtime_store.engine.begin() as connection:
        connection.execute(
            update(runtime_runs)
            .where(runtime_runs.c.id == run.id)
            .values(execution_contract={**run.execution_contract, "model_name": "tampered"})
        )

    repaired = runtime_store.get_run(run.id)
    assert repaired.status == RuntimeRunStatus.FAILED
    assert repaired.failure_category == RuntimeFailureCategory.CONTRACT_INTEGRITY
    frozen_once = runtime_store.get_frozen_business_projection(run.id)
    updated_once = runtime_store.investigation_repository.get("inv-1").updated_at

    again = runtime_store.get_run(run.id)

    # 修复必须一次性持久化：二次读取不得重 freeze、重写 Run 行或触碰业务投影。
    assert again.execution_contract == repaired.execution_contract
    assert runtime_store.get_frozen_business_projection(run.id) == frozen_once
    assert (
        runtime_store.investigation_repository.get("inv-1").updated_at == updated_once
    )
    with runtime_store.engine.begin() as connection:
        persisted_contract = connection.execute(
            select(runtime_runs.c.execution_contract).where(
                runtime_runs.c.id == run.id
            )
        ).scalar_one()
    assert persisted_contract == repaired.execution_contract


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


def test_v11_result_validation_without_round_is_rejected_before_persistence(
    runtime_store,
) -> None:
    execution = AgentExecution(
        id="execution-result-validation-no-round",
        task_id="task-result-validation-no-round",
        agent_name="CoordinatorAgent",
        step_kind=ExecutionStepKind.RESULT_VALIDATION,
        runtime_run_id="run-result-validation-no-round",
    ).model_copy(update={"runtime_run_id": None})

    with pytest.raises(ValueError, match="runtime_run_id"):
        runtime_store.investigation_repository.save_executions("inv-1", [execution])

    assert runtime_store.investigation_repository.list_executions("inv-1") == []


def _v11_task(task_id: str, runtime_run_id: str) -> DiagnosisTask:
    return DiagnosisTask(
        id=task_id,
        title="Trace the first failure",
        description="Find the earliest causal boundary.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name=FindingActor.INVESTIGATOR,
        analysis_round=1,
        runtime_run_id=runtime_run_id,
        evidence_scope={"entity_ids": ["checkout-service"]},
    )


def test_v11_task_save_revalidates_missing_owner_before_persistence(runtime_store) -> None:
    task = _v11_task("task-save-owner", "run-save-owner").model_copy(
        update={"runtime_run_id": None}
    )

    with pytest.raises(ValueError, match="runtime_run_id"):
        runtime_store.investigation_repository.save_tasks("inv-1", [task])

    assert runtime_store.investigation_repository.list_tasks("inv-1") == []


def test_v11_plan_save_revalidates_mixed_owner_before_persistence(runtime_store) -> None:
    task = _v11_task("task-plan-owner", "run-plan-owner")
    plan = DiagnosisPlan(
        id="plan-owner",
        investigation_id="inv-1",
        runtime_run_id="run-plan-owner",
        tasks=[task],
    ).model_copy(update={"runtime_run_id": "run-foreign"})

    with pytest.raises(ValueError, match="runtime_run_id"):
        runtime_store.investigation_repository.save_plan(plan)

    assert runtime_store.investigation_repository.get_plan("inv-1") is None


def test_v11_investigation_save_revalidates_nested_action_owner_before_persistence(
    runtime_store,
) -> None:
    action = RecommendedAction(
        id="action-owner",
        action_type=ActionType.CHECK,
        title="Check the service owner",
        description="Record a bounded follow-up.",
        risk_level=ActionRiskLevel.LOW,
        requires_approval=False,
        supporting_evidence_ids=["evidence-owner"],
        related_candidate_ids=["candidate-owner"],
        runtime_run_id="run-good",
    ).model_copy(update={"runtime_run_id": None})
    record = runtime_store.investigation_repository.get("inv-1").model_copy(
        update={"active_runtime_run_id": "run-good", "actions": [action]}
    )

    with pytest.raises(ValueError, match="runtime_run_id"):
        runtime_store.investigation_repository.save(record)

    restored = runtime_store.investigation_repository.get("inv-1")
    assert restored.active_runtime_run_id is None
    assert restored.actions == []


@pytest.mark.anyio
async def test_v11_intake_activates_projection_only_inside_commit_transaction(
    runtime_store, monkeypatch
) -> None:
    """RR-L1：INTAKE handler 不得在 commit 事务外独立提交 activate。

    SQLite 的 activate_projection 自带独立事务并即时提交，handler 提前调用会
    造成 owner 已切换、latest 投影已清理但无 INTAKE checkpoint 的崩溃窗口；
    激活只允许经由 commit_phase 事务内的 activate_projection_with_connection
    发生。memory 路径在共享锁内无该窗口，仅参数化回归 handler 行为。
    """
    repository = runtime_store.investigation_repository
    is_sqlite = hasattr(runtime_store, "engine")
    standalone_calls = []
    original_activate = repository.activate_projection

    def _spy_activate(*args, **kwargs):
        standalone_calls.append(args)
        return original_activate(*args, **kwargs)

    monkeypatch.setattr(repository, "activate_projection", _spy_activate)

    run = runtime_store.create_run(_v11_run())
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-v11",
        expected_status=RuntimeRunStatus.CREATED,
    )
    executor = DiagnosisPhaseExecutor(
        SimpleNamespace(repository=repository, agents_runtime=None)
    )
    executor._is_durable_session = True

    output = await executor.execute_phase(
        PhaseInput(
            run_id=run.id,
            attempt_id=attempt.id,
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(
                remaining_tool_budget=8, remaining_token_budget=1000
            ),
            investigation_id="inv-1",
            strategy=InvestigationStrategy.ADAPTIVE,
            execution_contract_version=ExecutionContractVersion.V11,
            tool_budget=8,
        )
    )

    if is_sqlite:
        # handler 返回前不得产生任何独立事务激活；memory 下共享锁内语义等价。
        assert standalone_calls == []
        # handler 返回后、commit 前，持久投影必须保持未切换（无窗口副作用）。
        persisted = repository.get("inv-1")
        assert persisted.active_runtime_run_id is None

    runtime_store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-v11",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=output.business_mutation,
            safe_payload=output.safe_payload,
            resume_state=RuntimeResumeState(
                remaining_tool_budget=8, remaining_token_budget=1000
            ),
        )
    )

    restored = repository.get("inv-1")
    assert restored.active_runtime_run_id == run.id
    assert restored.evidence == []
    assert restored.multi_agent_run is None
