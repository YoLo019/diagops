from datetime import UTC, datetime

from sqlalchemy import create_engine

from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import metadata
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.hypotheses import CauseType


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
