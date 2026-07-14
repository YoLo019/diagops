import json

import pytest
from pydantic import ValidationError

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
    ModelProvider,
    MultiAgentRunStatus,
    StabilizationCategory,
)


def test_agent_finding_requires_evidence_unless_gap():
    with pytest.raises(ValidationError, match="evidence_ids"):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.LOG,
            finding_type=AgentFindingType.SIGNAL,
            summary="Log error spike",
            confidence=0.8,
        )

    gap = AgentFinding(
        investigation_id="inv-1",
        agent_name=AgentName.LOG,
        finding_type=AgentFindingType.GAP,
        summary="Log evidence is missing",
        confidence=0.2,
    )

    assert gap.evidence_ids == []


def test_agent_finding_confidence_is_bounded():
    with pytest.raises(ValidationError):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.METRIC,
            finding_type=AgentFindingType.SIGNAL,
            summary="Bad confidence",
            confidence=1.1,
            evidence_ids=["ev-1"],
        )


def test_old_agent_finding_payload_gets_multi_agent_defaults():
    finding = AgentFinding.model_validate(
        {
            "investigation_id": "inv-1",
            "agent_name": "LogAgent",
            "finding_type": "signal",
            "summary": "Log error spike",
            "confidence": 0.8,
            "evidence_ids": ["ev-1"],
        }
    )

    assert finding.execution_layer == AgentExecutionLayer.CUSTOM
    assert finding.analysis_round == 1
    assert finding.revises_finding_id is None


def test_round_two_finding_requires_revision_id():
    with pytest.raises(ValidationError, match="revises_finding_id"):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.LOG,
            finding_type=AgentFindingType.SIGNAL,
            summary="Reconsidered signal",
            confidence=0.8,
            evidence_ids=["ev-1"],
            analysis_round=2,
        )


def test_round_one_finding_rejects_revision_id():
    with pytest.raises(ValidationError, match="revises_finding_id"):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.LOG,
            finding_type=AgentFindingType.SIGNAL,
            summary="Initial signal",
            confidence=0.8,
            evidence_ids=["ev-1"],
            analysis_round=1,
            revises_finding_id="finding-old",
        )


def test_coordination_review_orders_candidates_by_rank():
    lower = RootCauseCandidate(
        cause_type=CauseType.TRAFFIC_SPIKE,
        summary="Traffic may be elevated",
        rank=2,
        confidence=0.4,
        supporting_finding_ids=["finding-2"],
        supporting_evidence_ids=["ev-2"],
        rationale="Metric signal exists.",
        uncertainty="Deployment evidence is stronger.",
    )
    higher = RootCauseCandidate(
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="Deployment likely caused the incident",
        rank=1,
        confidence=0.8,
        supporting_finding_ids=["finding-1"],
        supporting_evidence_ids=["ev-1"],
        rationale="Deployment and log evidence align.",
        uncertainty="Metrics should be checked.",
    )

    review = CoordinationReview(investigation_id="inv-1", candidates=[lower, higher])

    assert [candidate.rank for candidate in review.candidates] == [1, 2]


def test_old_coordination_review_payload_gets_multi_agent_defaults():
    review = CoordinationReview.model_validate(
        {"investigation_id": "inv-1", "candidates": []}
    )
    another = CoordinationReview(investigation_id="inv-2")

    assert review.execution_layer == AgentExecutionLayer.CUSTOM
    assert review.run_status == MultiAgentRunStatus.COMPLETED
    assert review.decision_status is None
    assert review.baseline_cause_type is None
    assert review.selected_cause_type is None
    assert review.summary == ""
    assert review.uncertainty == ""
    assert review.model_provider is None
    assert review.model_name is None
    assert review.primary_stabilization_category is None
    assert review.secondary_stabilization_categories == []
    assert review.secondary_stabilization_categories is not (
        another.secondary_stabilization_categories
    )


def test_coordination_review_uses_pydantic_28_protected_namespaces():
    assert CoordinationReview.model_config["protected_namespaces"] == (
        "model_validate",
        "model_dump",
    )


def test_coordination_review_dumps_enum_values_as_json_strings():
    review = CoordinationReview(
        investigation_id="inv-1",
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        primary_stabilization_category=StabilizationCategory.GENUINE_CONFLICT,
        secondary_stabilization_categories=[StabilizationCategory.HYBRID_CONTRACT],
    )

    dumped = review.model_dump(mode="json")
    json.dumps(dumped)

    assert dumped["model_provider"] == "openai"
    assert dumped["primary_stabilization_category"] == "genuine_conflict"
    assert dumped["secondary_stabilization_categories"] == ["hybrid_contract"]
