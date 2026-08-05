from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from backend.config.settings import AppSettings, RuntimeSettings, StorageSettings
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import runtime_runs
from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)
from backend.domain.agent_findings import (
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    RootCauseCandidate,
)
from backend.domain.agent_plan import DiagnosisPlan, LeadDecision
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import (
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionContractVersion,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.runtime.diff import parse_frozen_projection
from backend.runtime.manager import RuntimeManager
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import (
    V10_PHASE_ORDER,
    V11_PHASE_ORDER,
    BusinessMutation,
    PhaseCommit,
    PhaseInput,
    phase_profile_for,
)
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeIntegrityError,
    RuntimeTerminalCommit,
    RuntimeTerminalEvent,
    validate_v11_phase_ownership,
)
from backend.services.container import AppContainer


def _contract() -> dict:
    return {
        "execution_contract_version": "v11",
        "authority_mode": "agent",
        "model_provider": "openai",
        "model_name": "gpt-test",
        "prompt_version": "v11-test",
        "tool_budget": 8,
        "token_budget": 1000,
        "timeout_seconds": 120.0,
    }


def _v11_run(run_id: str = "run-v11", **updates) -> RuntimeRun:
    values = {
        "id": run_id,
        "investigation_id": "inv-1",
        "run_kind": RuntimeRunKind.LIVE,
        "strategy": "adaptive",
        "run_reason": RuntimeRunReason.INITIAL,
        "model_provider": ModelProvider.OPENAI,
        "model_name": "gpt-test",
        "prompt_version": "v11-test",
        "tool_budget": 8,
        "token_budget": 1000,
        "timeout_seconds": 120.0,
        "execution_contract_version": ExecutionContractVersion.V11,
        "authority_mode": AuthorityMode.AGENT,
        "execution_contract": _contract(),
    }
    values.update(updates)
    return RuntimeRun(**values)


def test_phase_profiles_are_immutable_and_version_specific() -> None:
    assert [phase.value for phase in V10_PHASE_ORDER] == [
        "intake",
        "evidence_collection",
        "deterministic_rca",
        "specialist_analysis",
        "conflict_review",
        "coordination",
        "report_generation",
        "finalize",
    ]
    assert [phase.value for phase in V11_PHASE_ORDER] == [
        "intake",
        "evidence_collection",
        "lead_planning",
        "investigator_round_1",
        "critic_review",
        "investigator_round_2",
        "critic_reconciliation",
        "lead_adjudication",
        "result_validation",
        "report_generation",
        "finalize",
    ]
    assert phase_profile_for(ExecutionContractVersion.V10_LEGACY).order == V10_PHASE_ORDER
    assert phase_profile_for(ExecutionContractVersion.V11).order == V11_PHASE_ORDER
    assert V10_PHASE_ORDER is not V11_PHASE_ORDER


@pytest.mark.parametrize("storage_kind", ("memory", "sqlite"))
def test_legacy_to_v11_rerun_links_new_investigation_without_mutating_source(
    storage_kind: str, tmp_path
) -> None:
    storage_url = (
        "memory://"
        if storage_kind == "memory"
        else f"sqlite:///{tmp_path / 'linked-rerun.db'}"
    )
    container = AppContainer(AppSettings(storage=StorageSettings(url=storage_url)))
    repository = container.repository
    source = repository.save(
        InvestigationRecord(
            id="legacy-inv",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="legacy incident",
                description="legacy incident payload",
                started_at=datetime(2026, 8, 5, tzinfo=UTC),
            ),
        )
    )
    legacy_evidence = EvidenceItem(
        id="legacy-evidence",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="legacy projection",
    )
    source = repository.save(
        source.model_copy(
            update={
                "status": InvestigationStatus.COMPLETED,
                "evidence": [legacy_evidence],
            }
        )
    )
    source_payload = source.model_dump(mode="json")
    parent = container.runtime_store.create_run(
        RuntimeRun(
            id="legacy-run",
            investigation_id=source.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy="fixed",
            run_reason=RuntimeRunReason.INITIAL,
            status=RuntimeRunStatus.COMPLETED,
        )
    )

    run = container.create_runtime_run(
        source.id,
        strategy="adaptive",
        run_reason=RuntimeRunReason.MANUAL_RERUN,
        parent_run_id=parent.id,
        execution_contract_version=ExecutionContractVersion.V11,
    )

    assert run.investigation_id != source.id
    assert repository.get(source.id).model_dump(mode="json") == source_payload
    linked = repository.get(run.investigation_id)
    assert linked.source_investigation_id == source.id
    assert linked.event.model_dump(mode="json") == source.event.model_dump(mode="json")
    assert linked.evidence == []
    container.close()


@pytest.mark.anyio
async def test_v11_phase_handlers_trigger_no_legacy_diagnostic_sentinel(
    monkeypatch,
) -> None:
    repository = InMemoryInvestigationRepository()
    record = repository.save(
        InvestigationRecord(
            id="inv-v11-sentinel",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="V11 sentinel",
                description="forbidden legacy call guard",
                started_at=datetime(2026, 8, 5, tzinfo=UTC),
            ),
        )
    )


    def forbidden(*_args, **_kwargs):
        raise AssertionError("V11 invoked a forbidden legacy diagnostic path")

    orchestrator = SimpleNamespace(
        repository=repository,
        agents_runtime=None,
        max_total_tool_calls=8,
        analyzer=SimpleNamespace(analyze=forbidden),
        coordinator=SimpleNamespace(collect_async=forbidden),
        report_generator=SimpleNamespace(generate=forbidden),
        action_planner=SimpleNamespace(plan=forbidden),
        task_planner=SimpleNamespace(plan=forbidden),
        _record_v4_execution=forbidden,
        _record_v5_findings=forbidden,
        _record_v5_review=forbidden,
        _persist_adaptive_artifacts=forbidden,
        _validate_v7_result=forbidden,
        build_coordination_review=forbidden,
        build_hybrid_coordination_review=forbidden,
    )
    import backend.runtime.phase_executor as phase_executor_module

    monkeypatch.setattr(phase_executor_module, "validate_hypotheses", forbidden)
    executor = DiagnosisPhaseExecutor(orchestrator)
    executor._is_durable_session = True

    for phase in V11_PHASE_ORDER:
        await executor.execute_phase(
            PhaseInput(
                run_id="run-v11-sentinel",
                attempt_id="attempt-v11-sentinel",
                phase=phase,
                resume_state=RuntimeResumeState(remaining_tool_budget=8),
                investigation_id=record.id,
                strategy=InvestigationStrategy.ADAPTIVE,
                execution_contract_version=ExecutionContractVersion.V11,
                tool_budget=8,
            )
        )


@pytest.mark.anyio
async def test_direct_v11_phase_execution_requires_persisted_runtime_run() -> None:
    executor = DiagnosisPhaseExecutor(SimpleNamespace())
    with pytest.raises(ValueError, match="persisted RuntimeRun"):
        await executor.execute(
            IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="direct V11",
                description="direct V11 entry",
                started_at=datetime(2026, 8, 5, tzinfo=UTC),
            ),
            execution_contract_version=ExecutionContractVersion.V11,
        )


@pytest.mark.anyio
async def test_v11_service_entry_rejects_runtime_disabled() -> None:
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            runtime=RuntimeSettings(enabled=False),
        )
    )
    with pytest.raises(RuntimeConflict, match="enabled Runtime"):
        await container.run_investigation(
            object(),
            execution_contract_version=ExecutionContractVersion.V11,
        )
    container.close()


def test_v11_run_contract_round_trips_through_runtime_store(runtime_store) -> None:
    created = runtime_store.create_run(_v11_run())
    restored = runtime_store.get_run(created.id)

    assert restored.execution_contract_version == ExecutionContractVersion.V11
    assert restored.authority_mode == AuthorityMode.AGENT
    assert restored.execution_contract == _contract()


def test_v11_contract_projection_mismatch_terminally_persists_integrity_error(
    runtime_store,
) -> None:
    if not hasattr(runtime_store, "engine"):
        pytest.skip("memory store has no persisted compatibility columns")
    run = runtime_store.create_run(_v11_run(run_id="run-contract-mismatch"))

    broken = {**run.execution_contract, "model_name": "different-model"}
    with runtime_store.engine.begin() as connection:
        connection.execute(
            update(runtime_runs)
            .where(runtime_runs.c.id == run.id)
            .values(execution_contract=broken)
        )

    restored = runtime_store.get_run(run.id)
    assert restored.status == RuntimeRunStatus.FAILED
    assert restored.failure_category.value == "contract_integrity"
    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "not_activated"


def test_v11_later_commit_requires_active_projection_owner(runtime_store) -> None:
    run = runtime_store.create_run(_v11_run(run_id="run-owner-gate"))
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

    with pytest.raises(RuntimeIntegrityError, match="active projection"):
        runtime_store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-v11",
                lease_version=leased.lease_version,
                phase=RuntimePhase.EVIDENCE_COLLECTION,
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={},
                resume_state=RuntimeResumeState(
                    remaining_tool_budget=8,
                    remaining_token_budget=1000,
                ),
            )
        )


def test_v11_business_payloads_require_runtime_owner() -> None:
    run = _v11_run(run_id="run-owner-payload")
    commit = SimpleNamespace(
        phase=RuntimePhase.EVIDENCE_COLLECTION,
        business_mutation=BusinessMutation(
            investigation_id="inv-1",
            plan=DiagnosisPlan(investigation_id="inv-1"),
        ),
    )
    record = SimpleNamespace(active_runtime_run_id=run.id)

    with pytest.raises(RuntimeIntegrityError, match="owner"):
        validate_v11_phase_ownership(run, commit, record)


def test_v11_human_projection_updates_require_active_owner(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1").model_copy(
        update={
            "active_runtime_run_id": "run-active",
            "actions": [
                RecommendedAction(
                    id="action-owner",
                    action_type=ActionType.CHECK,
                    title="Check owner",
                    description="Verify that the update is owner-bound.",
                    risk_level=ActionRiskLevel.READ_ONLY,
                    requires_approval=False,
                    supporting_evidence_ids=["ev-1"],
                )
            ],
            "verification_suggestions": [
                VerificationSuggestion(
                    id="verification-owner",
                    title="Verify owner",
                    description="Verify that the update is owner-bound.",
                    expected_signal="owner matches",
                )
            ],
        }
    )
    repository.save(record)

    with pytest.raises(ValueError, match="owner"):
        repository.update_action_status(
            "inv-1",
            "action-owner",
            status=ActionStatus.DONE,
        )
    with pytest.raises(ValueError, match="owner"):
        repository.update_verification_status(
            "inv-1",
            "verification-owner",
            status=VerificationStatus.SKIPPED,
        )


def test_v11_frozen_projection_retains_authority_candidates_critics_and_usage(
    runtime_store,
) -> None:
    run_id = "run-v11-frozen"
    candidate = RootCauseCandidate(
        id="candidate-1",
        summary="bounded candidate",
        rank=1,
        confidence=0.8,
        affected_entity="checkout-api",
        failure_mechanism="deployment mismatch",
        onset_window_start=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
        onset_window_end=datetime(2026, 8, 5, 0, 5, tzinfo=UTC),
    )
    assessment = CriticAssessment(
        id="assessment-1",
        candidate_id=candidate.id,
        verdict=CriticVerdict.REJECT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="bounded gap",
                gap="missing evidence",
            )
            for name in CausalCheckName
        ],
        summary="critic summary",
        runtime_run_id=run_id,
    )
    lead = LeadDecision(
        action=LeadAction.INCONCLUSIVE,
        summary="insufficient evidence",
        stop_reason="insufficient evidence",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision=lead,
        diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
        runtime_run_id=run_id,
        authority_mode=AuthorityMode.AGENT,
    )
    report = IncidentReport(
        investigation_id="inv-1",
        summary="bounded report",
        markdown="bounded report",
        diagnoses=[candidate],
        diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
        authority_mode=AuthorityMode.AGENT,
        critic_assessments=[assessment],
        runtime_run_id=run_id,
    )
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1").model_copy(
        update={
            "active_runtime_run_id": run_id,
            "multi_agent_run": MultiAgentRunSummary(
                status=MultiAgentRunStatus.COMPLETED,
                diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
                authority_mode=AuthorityMode.AGENT,
                runtime_run_id=run_id,
                total_input_tokens=13,
                total_output_tokens=21,
                elapsed_time_ms=34,
            ),
            "report": report,
        }
    )
    repository.save(record)
    repository.save_multi_agent_result("inv-1", [], [], review)
    run = runtime_store.create_run(
        _v11_run(run_id=run_id, status=RuntimeRunStatus.COMPLETED)
    )

    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "activated"
    assert frozen.runtime_run_id == run.id
    assert frozen.authority_mode == "agent"
    assert frozen.review is not None
    assert frozen.review.candidates[0].affected_entity == "checkout-api"
    assert frozen.review.candidates[0].failure_mechanism == "deployment mismatch"
    assert frozen.review.lead_decision == lead.model_dump(mode="json")
    assert frozen.review.critic_assessments[0]["candidate_id"] == candidate.id
    assert frozen.usage == {
        "input_tokens": 13,
        "output_tokens": 21,
        "elapsed_time_ms": 34,
    }


def test_v11_intake_atomically_activates_owner_and_clears_latest_projection(
    runtime_store,
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    old_evidence = EvidenceItem(
        id="ev-old",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="old latest evidence",
    )
    repository.save(record.model_copy(update={"evidence": [old_evidence]}))
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

    record = repository.get("inv-1").model_copy(
        update={"active_runtime_run_id": run.id, "evidence": []}
    )
    runtime_store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-v11",
            lease_version=leased.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(
                investigation_id="inv-1",
                investigation=record,
                activate_projection=True,
            ),
            safe_payload={"status": "completed"},
            resume_state=RuntimeResumeState(
                remaining_tool_budget=8,
                remaining_token_budget=1000,
            ),
        )
    )

    restored = repository.get("inv-1")
    assert restored.active_runtime_run_id == run.id
    assert restored.evidence == []
    assert restored.hypotheses == []
    assert restored.report is None


def test_pre_intake_v11_cancel_preserves_previous_owner_projection(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    old_evidence = EvidenceItem(
        id="old-owner-evidence",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="previous owner projection",
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": "old-owner-run",
                "status": InvestigationStatus.COMPLETED,
                "evidence": [old_evidence],
            }
        )
    )
    run = runtime_store.create_run(_v11_run(run_id="run-cancel-before-intake"))

    runtime_store.request_cancel(run.id)

    restored = repository.get("inv-1")
    assert restored.active_runtime_run_id == "old-owner-run"
    assert restored.status == InvestigationStatus.COMPLETED
    assert [item.id for item in restored.evidence] == [old_evidence.id]
    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "not_activated"


def test_pre_intake_v11_failure_preserves_previous_owner_projection(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    old_evidence = EvidenceItem(
        id="old-owner-failure-evidence",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="previous owner projection",
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": "old-owner-run",
                "status": InvestigationStatus.COMPLETED,
                "evidence": [old_evidence],
            }
        )
    )
    run = runtime_store.create_run(_v11_run(run_id="run-failure-before-intake"))
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

    runtime_store.commit_terminal(
        RuntimeTerminalCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-v11",
            lease_version=leased.lease_version,
            expected_run_status=RuntimeRunStatus.RUNNING,
            target_run_status=RuntimeRunStatus.FAILED,
            expected_attempt_status=RuntimeAttemptStatus.RUNNING,
            target_attempt_status=RuntimeAttemptStatus.FAILED,
            failure_category=RuntimeFailureCategory.UNKNOWN,
            events=(
                RuntimeTerminalEvent(
                    event_type=RuntimeEventType.RUN_FAILED,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={"status": "failed"},
                ),
            ),
        )
    )

    restored = repository.get("inv-1")
    assert restored.active_runtime_run_id == "old-owner-run"
    assert restored.status == InvestigationStatus.COMPLETED
    assert [item.id for item in restored.evidence] == [old_evidence.id]
    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "not_activated"


def test_pre_intake_v11_terminal_run_has_not_activated_projection(runtime_store) -> None:
    run = runtime_store.create_run(
        _v11_run(
            run_id="run-never-activated",
            status=RuntimeRunStatus.CANCELLED,
        )
    )

    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "not_activated"
    assert frozen.evidence == []
    assert frozen.findings == []
    assert frozen.report is None


def test_v11_recovery_rejection_freezes_without_inheriting_active_owner(
    runtime_store,
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    old_evidence = EvidenceItem(
        id="ev-active-owner",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="active owner evidence",
    )
    repository.save(
        record.model_copy(
            update={"active_runtime_run_id": "run-active-owner", "evidence": [old_evidence]}
        )
    )
    run = runtime_store.create_run(_v11_run(run_id="run-recovery-rejected"))
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
    runtime_store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-v11",
        lease_version=leased.lease_version,
    )
    runtime_store.append_recovery_rejection(
        run.id,
        attempt_id=attempt.id,
        checkpoint_id=None,
    )

    frozen = parse_frozen_projection(
        runtime_store.get_frozen_business_projection(run.id)
    )
    assert frozen.projection_state == "not_activated"
    assert frozen.runtime_run_id == run.id
    assert frozen.evidence == []
    assert frozen.review is None
    assert frozen.report is None


@pytest.mark.anyio
async def test_runtime_manager_rejects_interrupted_legacy_recovery(runtime_store) -> None:
    run = runtime_store.create_run(
        RuntimeRun(
            id="run-legacy-recovery",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy="fixed",
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-legacy",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="worker-legacy",
        lease_version=leased.lease_version,
    )

    class Coordinator:
        store = runtime_store

    manager = RuntimeManager(coordinator_factory=lambda _run_id: Coordinator())
    with pytest.raises(RuntimeConflict, match="V11"):
        await manager.resume(run.id)

    assert runtime_store.list_events(run.id)[-1].event_type.value == "recovery.rejected"
