from datetime import UTC, datetime

import pytest

from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_agent_semantics,
)
from backend.diagnosis.result_validation import validate_v11_result
from backend.domain.agent_findings import (
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    CriticVerdict,
    RootCauseAttribution,
    RootCauseCandidate,
)
from backend.domain.agent_plan import LeadDecision
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import (
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    DiagnosticStatus,
    LeadAction,
)


def test_root_cause_attribution_rejects_unknown_evidence():
    evidence = EvidenceItem(
        id="ev-known",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
        summary="known evidence",
    )
    attribution = RootCauseAttribution(
        root_cause_occurred_at=evidence.timestamp,
        root_cause_component="checkout",
        root_cause_reason="process failure",
        supporting_evidence_ids=["ev-missing"],
    )

    with pytest.raises(EvidenceContractError, match="unknown_or_unusable_evidence"):
        validate_agent_semantics([evidence], [], [], [attribution])


def test_v11_validator_is_mechanical_and_does_not_rewrite_candidate_fields():
    evidence = EvidenceItem(
        id="ev-log",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
        summary="bounded log evidence",
    )
    candidate = RootCauseCandidate(
        id="candidate-1",
        cause_type="traffic_spike",
        affected_entity="checkout-api",
        failure_mechanism="queue saturation",
        summary="candidate without provider semantic support",
        rank=1,
        confidence=0.7,
        supporting_evidence_ids=[evidence.id],
        contradicting_evidence_ids=[],
        onset_window_start=datetime(2026, 7, 15, 0, tzinfo=UTC),
        onset_window_end=datetime(2026, 7, 15, 1, tzinfo=UTC),
    )
    checks = [
        CausalCheck(
            name=name,
            status=CausalCheckStatus.PASS,
            summary="committed reference",
            evidence_ids=[evidence.id],
        )
        for name in CausalCheckName
    ]
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[
            CriticAssessment(
                candidate_id=candidate.id,
                verdict=CriticVerdict.ACCEPT,
                checks=checks,
                summary="seven mechanical checks",
                runtime_run_id="run-v11",
            )
        ],
        lead_decision=LeadDecision(
            action=LeadAction.INCONCLUSIVE,
            summary="do not conclude",
            stop_reason="manual hold",
        ),
        diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
    )
    before = candidate.model_dump(mode="json")

    validate_v11_result(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        findings=[],
        candidates=review.candidates,
        review=review,
        evidence=[evidence],
        status=DiagnosticStatus.INCONCLUSIVE,
    )

    assert candidate.model_dump(mode="json") == before
