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
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadDecision,
)
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionStepKind,
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
            runtime_run_id="run-v11",
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
        review=None,
        evidence=[evidence],
        status=None,
    )

    assert candidate.model_dump(mode="json") == before


def _validation_evidence(*, runtime_run_id: str = "run-v11", evidence_id: str = "ev-log"):
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        status=EvidenceStatus.SUCCESS,
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
        summary="committed evidence",
        runtime_run_id=runtime_run_id,
    )


def _validation_candidate(evidence_id: str = "ev-log") -> RootCauseCandidate:
    return RootCauseCandidate(
        id="candidate-1",
        summary="candidate",
        rank=1,
        confidence=0.7,
        supporting_evidence_ids=[evidence_id],
    )


def _validation_checks(evidence_id: str = "ev-log", unsafe: bool = False):
    return [
        CausalCheck(
            name=name,
            status=CausalCheckStatus.PASS,
            summary="bad\x00check" if unsafe and index == 0 else "supported",
            evidence_ids=[evidence_id],
        )
        for index, name in enumerate(CausalCheckName)
    ]


def _validation_execution(
    *, actor: str, step_kind: ExecutionStepKind
) -> AgentExecution:
    return AgentExecution(
        task_id=f"task-{step_kind.value}",
        agent_name=actor,
        runtime_run_id="run-v11",
        status=AgentExecutionStatus.COMPLETED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=step_kind,
        summary="completed",
    )


def test_v11_validator_rejects_evidence_from_another_run():
    evidence = _validation_evidence(runtime_run_id="run-other")
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(),
        summary="assessed",
        runtime_run_id="run-v11",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
    )

    with pytest.raises(Exception, match="evidence"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            evidence=[evidence],
        )


def test_v11_partial_requires_usable_evidence_passing_check_and_round_two_linkage():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(),
        summary="assessed",
        runtime_run_id="run-v11",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision=LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="conclude",
            candidate_ids=[candidate.id],
        ),
        diagnostic_status=DiagnosticStatus.PARTIAL,
    )
    executions = [
        _validation_execution(
            actor=ExecutionActor.CRITIC.value,
            step_kind=ExecutionStepKind.CRITIC_REVIEW,
        ),
        _validation_execution(
            actor=ExecutionActor.LEAD.value,
            step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
        ),
    ]

    with pytest.raises(Exception, match="partial"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            executions=executions,
            evidence=[evidence],
            status=DiagnosticStatus.PARTIAL,
        )


def test_v11_partial_requires_final_round_critic_and_lead_audits():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(),
        summary="reconciled",
        runtime_run_id="run-v11",
        review_round=2,
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision=LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="conclude",
            candidate_ids=[candidate.id],
        ),
        diagnostic_status=DiagnosticStatus.PARTIAL,
    )
    task = DiagnosisTask(
        id="task-round-two",
        title="collect supplemental evidence",
        description="collect the requested signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=2,
        evidence_scope={"entity_ids": ["checkout-service"]},
        runtime_run_id="run-v11",
        critic_assessment_id=assessment.id,
    )
    executions = [
        _validation_execution(
            actor=ExecutionActor.CRITIC.value,
            step_kind=ExecutionStepKind.CRITIC_REVIEW,
        ),
        _validation_execution(
            actor=ExecutionActor.LEAD.value,
            step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
        ),
    ]

    with pytest.raises(Exception, match="final_critic"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            executions=executions,
            evidence=[evidence],
            tasks=[task],
            status=DiagnosticStatus.PARTIAL,
        )


def test_v11_validator_scans_nested_critic_check_text_for_control_characters():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(unsafe=True),
        summary="assessed",
        runtime_run_id="run-v11",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
    )

    with pytest.raises(Exception, match="unsafe_text"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            evidence=[evidence],
        )


def test_v11_complete_requires_final_critic_and_lead_execution_audit():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(),
        summary="assessed",
        runtime_run_id="run-v11",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision=LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="conclude",
            candidate_ids=[candidate.id],
        ),
        diagnostic_status=DiagnosticStatus.COMPLETE,
    )

    with pytest.raises(Exception, match="execution"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            evidence=[evidence],
            status=DiagnosticStatus.COMPLETE,
        )
