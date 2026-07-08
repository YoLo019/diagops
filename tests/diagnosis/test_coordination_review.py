from datetime import UTC, datetime

import pytest

from backend.diagnosis.coordination_review import build_coordination_review
from backend.domain.agent_findings import AgentFinding, AgentFindingType, AgentName
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis


def test_multiple_findings_for_same_cause_rank_first_with_supporting_ids():
    findings = [
        _finding(
            "finding-log",
            AgentName.LOG,
            AgentFindingType.ROOT_CAUSE,
            CauseType.DEPLOYMENT_REGRESSION,
            0.7,
            ["ev-log"],
            "Log error started after deploy",
        ),
        _finding(
            "finding-deploy",
            AgentName.DEPLOYMENT,
            AgentFindingType.ROOT_CAUSE,
            CauseType.DEPLOYMENT_REGRESSION,
            0.8,
            ["ev-deploy"],
            "Deploy changed checkout",
        ),
        _finding(
            "finding-metric",
            AgentName.METRIC,
            AgentFindingType.ROOT_CAUSE,
            CauseType.TRAFFIC_SPIKE,
            0.6,
            ["ev-metric"],
            "Traffic rose",
        ),
    ]

    review = build_coordination_review(
        "inv-1",
        findings,
        [_evidence("ev-log"), _evidence("ev-deploy"), _evidence("ev-metric")],
        [],
    )

    top = review.candidates[0]
    assert top.rank == 1
    assert top.cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert top.supporting_finding_ids == ["finding-log", "finding-deploy"]
    second = review.candidates[1]
    assert second.rank == 2
    assert "candidate #2" in second.summary


def test_unknown_finding_evidence_id_raises_value_error():
    finding = _finding(
        "finding-log",
        AgentName.LOG,
        AgentFindingType.ROOT_CAUSE,
        CauseType.DATABASE_SLOWDOWN,
        0.7,
        ["ev-missing"],
        "Slow queries",
    )

    with pytest.raises(ValueError, match="Unknown evidence id"):
        build_coordination_review("inv-1", [finding], [_evidence("ev-known")], [])


def test_no_findings_seeds_candidates_from_hypotheses_with_limited_uncertainty():
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            summary="Payments timed out",
            confidence=0.6,
            supporting_evidence_ids=["ev-support"],
            contradicting_evidence_ids=["ev-against"],
            next_actions=["Check dependency status"],
        )
    ]

    review = build_coordination_review(
        "inv-1",
        [],
        [_evidence("ev-support"), _evidence("ev-against")],
        hypotheses,
    )

    assert len(review.candidates) == 1
    candidate = review.candidates[0]
    assert candidate.cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE
    assert candidate.rank == 1
    assert candidate.confidence == 0.6
    assert candidate.supporting_evidence_ids == ["ev-support"]
    assert candidate.contradicting_evidence_ids == ["ev-against"]
    assert "limited specialist findings" in candidate.uncertainty


def test_unknown_hypothesis_evidence_id_raises_value_error_without_findings():
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.UNKNOWN,
            summary="Unknown seed",
            confidence=0.4,
            supporting_evidence_ids=["ev-missing"],
        )
    ]

    with pytest.raises(ValueError, match="Unknown evidence id"):
        build_coordination_review("inv-1", [], [_evidence("ev-known")], hypotheses)


def _finding(
    finding_id: str,
    agent_name: AgentName,
    finding_type: AgentFindingType,
    cause_type: CauseType,
    confidence: float,
    evidence_ids: list[str],
    summary: str,
) -> AgentFinding:
    return AgentFinding(
        id=finding_id,
        investigation_id="inv-1",
        agent_name=agent_name,
        finding_type=finding_type,
        summary=summary,
        confidence=confidence,
        evidence_ids=evidence_ids,
        related_cause_type=cause_type,
    )


def _evidence(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=f"{evidence_id} summary",
    )
