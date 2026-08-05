from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import agent_findings, coordination_reviews, metadata
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    LeadDecision,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AdaptiveRunStatus,
    AdaptiveStopReason,
    AgentExecutionLayer,
    AuthorityMode,
    CoordinationDecisionStatus,
    DiagnosticStatus,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
    StabilizationCategory,
)


def build_sqlite_repository():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return SQLiteInvestigationRepository(engine)


def dt(minutes: int) -> datetime:
    return datetime(2026, 7, 8, 9, minutes, tzinfo=UTC)


def test_sqlite_investigation_strategy_round_trips_without_schema_change():
    repository = build_sqlite_repository()
    record = InvestigationRecord(
        event=IncidentEvent(
            source=IncidentSource.MANUAL,
            service="checkout",
            environment="production",
            severity=Severity.CRITICAL,
            title="Checkout errors",
            description="Error rate increased",
            started_at=dt(0),
        ),
        strategy=InvestigationStrategy.ADAPTIVE,
        multi_agent_run=MultiAgentRunSummary(
            status=MultiAgentRunStatus.COMPLETED,
            strategy=InvestigationStrategy.ADAPTIVE,
            adaptive_status=AdaptiveRunStatus.COMPLETED,
            adaptive_stop_reason=AdaptiveStopReason.SUFFICIENT_EVIDENCE,
            tool_call_count=2,
            max_tool_calls_per_specialist=4,
            max_total_tool_calls=9,
        ),
    )

    repository.save(record)

    persisted = repository.get(record.id)
    assert persisted.strategy == InvestigationStrategy.ADAPTIVE
    assert persisted.multi_agent_run == record.multi_agent_run


@pytest.mark.parametrize("repository_kind", ["memory", "sqlite"])
def test_v11_summary_projection_round_trips_owner_and_status(
    repository_kind, tmp_path
):
    if repository_kind == "memory":
        repository = InMemoryInvestigationRepository()
    else:
        repository = build_sqlite_repository()
    record = InvestigationRecord(
        event=IncidentEvent(
            source=IncidentSource.MANUAL,
            service="checkout",
            environment="production",
            severity=Severity.WARNING,
            title="Checkout summary",
            description="Summary projection",
            started_at=dt(0),
        ),
        strategy=InvestigationStrategy.ADAPTIVE,
        multi_agent_run=MultiAgentRunSummary(
            status=MultiAgentRunStatus.COMPLETED,
            diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
            authority_mode=AuthorityMode.AGENT,
            runtime_run_id="run-v11",
        ),
        active_runtime_run_id="run-v11",
    )
    repository.save(record)

    summary = repository.list_summaries()[0]
    assert summary.strategy == InvestigationStrategy.ADAPTIVE
    assert summary.active_runtime_run_id == "run-v11"
    assert summary.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    assert summary.authority_mode == AuthorityMode.AGENT


def finding(
    finding_id: str,
    agent_name: AgentName = AgentName.LOG,
    created_at: datetime | None = None,
) -> AgentFinding:
    return AgentFinding(
        id=finding_id,
        investigation_id="inv-1",
        agent_name=agent_name,
        finding_type=AgentFindingType.SIGNAL,
        summary=f"Finding {finding_id}",
        confidence=0.8,
        evidence_ids=[f"ev-{finding_id}"],
        related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
        created_at=created_at or dt(1),
    )


def review(review_id: str, confidence: float = 0.9) -> CoordinationReview:
    return CoordinationReview(
        id=review_id,
        investigation_id="inv-1",
        candidates=[
            RootCauseCandidate(
                id="candidate-1",
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="Deployment likely caused the incident",
                rank=1,
                confidence=confidence,
                supporting_finding_ids=["finding-1"],
                supporting_evidence_ids=["ev-finding-1"],
            )
        ],
        created_at=dt(3),
    )


def attributed_execution(execution_id: str = "execution-v8-1") -> AgentExecution:
    return AgentExecution(
        id=execution_id,
        task_id="task-v8-1",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.FINAL_SYNTHESIS,
        attempt=1,
        failure_category=FailureCategory.INVALID_OUTPUT,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        started_at=dt(2),
    )


def attributed_review(review_id: str = "review-v8-1") -> CoordinationReview:
    return review(review_id).model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "run_status": MultiAgentRunStatus.PARTIAL,
            "model_provider": ModelProvider.OPENAI,
            "model_name": "gpt-test",
            "primary_stabilization_category": StabilizationCategory.FINAL_SYNTHESIS,
            "secondary_stabilization_categories": [
                StabilizationCategory.SPECIALIST_OUTPUT_CONTRACT
            ],
        }
    )


@pytest.mark.parametrize("repository_kind", ["memory", "sqlite"])
def test_v8_1_multi_agent_result_round_trips_atomically(
    repository_kind, tmp_path
):
    if repository_kind == "memory":
        repository = InMemoryInvestigationRepository()

        def reopen():
            return repository
    else:
        database_path = tmp_path / "v8-1-result.db"
        engine = create_engine(f"sqlite:///{database_path}")
        metadata.create_all(engine)
        repository = SQLiteInvestigationRepository(engine)

        def reopen():
            engine.dispose()
            return SQLiteInvestigationRepository(
                create_engine(f"sqlite:///{database_path}")
            )

    saved_finding = finding("finding-v8-1")
    saved_execution = attributed_execution()
    saved_review = attributed_review()

    repository.save_multi_agent_result(
        "inv-1", [saved_finding], [saved_execution], saved_review
    )
    reloaded = reopen()

    assert reloaded.list_agent_findings("inv-1") == [saved_finding]
    assert reloaded.list_executions("inv-1") == [saved_execution]
    assert reloaded.get_coordination_review("inv-1") == saved_review


def test_sqlite_multi_agent_result_rolls_back_rows_when_review_insert_fails():
    class FailingReviewRepository(SQLiteInvestigationRepository):
        def _insert_multi_agent_review(self, connection, row):
            del connection, row
            raise RuntimeError("injected review insert failure")

    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    repository = FailingReviewRepository(engine)
    original_review = review("review-existing")
    repository.save_coordination_review(original_review)

    with pytest.raises(RuntimeError, match="injected review insert failure"):
        repository.save_multi_agent_result(
            "inv-1",
            [finding("finding-v8-1")],
            [attributed_execution()],
            attributed_review(),
        )

    assert repository.list_agent_findings("inv-1") == []
    assert repository.list_executions("inv-1") == []
    assert repository.get_coordination_review("inv-1") == original_review


@pytest.mark.parametrize("repository_kind", ["memory", "sqlite"])
def test_v11_multi_agent_result_rejects_missing_payload_owners(repository_kind):
    repository = (
        InMemoryInvestigationRepository()
        if repository_kind == "memory"
        else build_sqlite_repository()
    )
    v11_review = review("review-v11-owner").model_copy(
        update={
            "authority_mode": AuthorityMode.AGENT,
            "runtime_run_id": "run-v11",
            "lead_decision": LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="No safe conclusion.",
                stop_reason="evidence ended",
            ),
        }
    )

    with pytest.raises(ValueError, match="owner"):
        repository.save_multi_agent_result(
            "inv-1",
            [finding("finding-v11-owner")],
            [attributed_execution("execution-v11-owner")],
            v11_review,
        )


def test_in_memory_repository_saves_lists_and_replaces_agentic_rca_payloads():
    repository = InMemoryInvestigationRepository()
    first = finding("finding-1", created_at=dt(1))
    second = finding("finding-2", AgentName.METRIC, created_at=dt(2))
    updated_first = first.model_copy(update={"summary": "Updated log signal"})

    assert repository.save_agent_findings("inv-1", [first]) == [first]
    repository.save_agent_findings("inv-1", [second])
    repository.save_agent_findings("inv-1", [updated_first])

    assert repository.list_agent_findings("inv-1") == [updated_first, second]
    assert repository.list_agent_findings("missing") == []

    first_review = review("review-1")
    replacement_review = review("review-2", confidence=0.7)

    assert repository.get_coordination_review("inv-1") is None
    assert repository.save_coordination_review(first_review) == first_review
    repository.save_coordination_review(replacement_review)

    assert repository.get_coordination_review("inv-1") == replacement_review


def test_sqlite_repository_saves_lists_and_replaces_agentic_rca_payloads():
    repository = build_sqlite_repository()
    first = finding("finding-1", created_at=dt(1))
    second = finding("finding-2", AgentName.METRIC, created_at=dt(2))
    updated_first = first.model_copy(update={"summary": "Updated log signal"})

    assert repository.save_agent_findings("inv-1", [first]) == [first]
    repository.save_agent_findings("inv-1", [second])
    repository.save_agent_findings("inv-1", [updated_first])

    assert repository.list_agent_findings("inv-1") == [updated_first, second]
    assert repository.list_agent_findings("missing") == []

    first_review = review("review-1")
    replacement_review = review("review-2", confidence=0.7)

    assert repository.get_coordination_review("inv-1") is None
    assert repository.save_coordination_review(first_review) == first_review
    repository.save_coordination_review(replacement_review)

    assert repository.get_coordination_review("inv-1") == replacement_review


@pytest.mark.parametrize(
    "repository",
    [InMemoryInvestigationRepository(), build_sqlite_repository()],
)
def test_v7_findings_and_partial_review_round_trip(repository):
    round_one = finding("finding-round-1").model_copy(
        update={"execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK}
    )
    round_two = finding("finding-round-2", created_at=dt(2)).model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "analysis_round": 2,
            "revises_finding_id": round_one.id,
        }
    )
    partial_review = review("review-v7").model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "run_status": MultiAgentRunStatus.PARTIAL,
            "decision_status": CoordinationDecisionStatus.CONFLICT,
            "baseline_cause_type": CauseType.DEPLOYMENT_REGRESSION,
            "selected_cause_type": CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            "summary": "Agents disagree on the leading cause.",
            "uncertainty": "Metrics are incomplete.",
        }
    )

    repository.save_agent_findings("inv-1", [round_one, round_two])
    repository.save_coordination_review(partial_review)

    assert repository.list_agent_findings("inv-1") == [round_one, round_two]
    assert repository.get_coordination_review("inv-1") == partial_review


def test_old_agentic_rca_payloads_use_v7_defaults():
    old_finding = finding("finding-old").model_dump(
        mode="json",
        exclude={"execution_layer", "analysis_round", "revises_finding_id"},
    )
    old_review = review("review-old").model_dump(
        mode="json",
        exclude={
            "execution_layer",
            "run_status",
            "decision_status",
            "baseline_cause_type",
            "selected_cause_type",
            "summary",
            "uncertainty",
        },
    )

    restored_finding = AgentFinding.model_validate(old_finding)
    restored_review = CoordinationReview.model_validate(old_review)

    assert restored_finding.execution_layer == AgentExecutionLayer.CUSTOM
    assert restored_finding.analysis_round == 1
    assert restored_finding.revises_finding_id is None
    assert restored_review.execution_layer == AgentExecutionLayer.CUSTOM
    assert restored_review.run_status == MultiAgentRunStatus.COMPLETED
    assert restored_review.decision_status is None
    assert restored_review.baseline_cause_type is None
    assert restored_review.selected_cause_type is None
    assert restored_review.summary == ""
    assert restored_review.uncertainty == ""

    memory_repository = InMemoryInvestigationRepository()
    memory_repository.save_agent_findings("inv-1", [restored_finding])
    memory_repository.save_coordination_review(restored_review)
    assert memory_repository.list_agent_findings("inv-1") == [restored_finding]
    assert memory_repository.get_coordination_review("inv-1") == restored_review

    sqlite_repository = build_sqlite_repository()
    with sqlite_repository.engine.begin() as connection:
        connection.execute(
            insert(agent_findings).values(
                id=old_finding["id"],
                investigation_id="inv-1",
                agent_name=old_finding["agent_name"],
                created_at=old_finding["created_at"],
                payload=old_finding,
            )
        )
        connection.execute(
            insert(coordination_reviews).values(
                id=old_review["id"],
                investigation_id="inv-1",
                created_at=old_review["created_at"],
                payload=old_review,
            )
        )
    assert sqlite_repository.list_agent_findings("inv-1") == [restored_finding]
    assert sqlite_repository.get_coordination_review("inv-1") == restored_review
