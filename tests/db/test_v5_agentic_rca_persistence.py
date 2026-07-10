from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert

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
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
)


def build_sqlite_repository():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return SQLiteInvestigationRepository(engine)


def dt(minutes: int) -> datetime:
    return datetime(2026, 7, 8, 9, minutes, tzinfo=UTC)


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
