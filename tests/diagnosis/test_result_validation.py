from datetime import UTC, datetime

import pytest

from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_agent_semantics,
)
from backend.diagnosis.result_validation import validate_v11_result
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    CriticVerdict,
    FindingActor,
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
    *, actor: str, step_kind: ExecutionStepKind, analysis_round: int | None = None
) -> AgentExecution:
    return AgentExecution(
        task_id=f"task-{step_kind.value}",
        agent_name=actor,
        runtime_run_id="run-v11",
        status=AgentExecutionStatus.COMPLETED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        analysis_round=analysis_round,
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


def test_v11_validator_rejects_orphan_supplemental_task_ids():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=_validation_checks(),
        gap="need a round two signal",
        supplemental_task_ids=["ghost-task"],
        summary="needs more evidence",
        runtime_run_id="run-v11",
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[assessment],
    )
    persisted_task = DiagnosisTask(
        id="real-round-two-task",
        title="collect the requested signal",
        description="collect the requested signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name=ExecutionActor.INVESTIGATOR.value,
        analysis_round=2,
        evidence_scope={"entity_ids": ["checkout-service"]},
        runtime_run_id="run-v11",
        critic_assessment_id=assessment.id,
    )

    with pytest.raises(Exception, match="supplemental"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[],
            candidates=[candidate],
            review=review,
            evidence=[evidence],
            tasks=[persisted_task],
        )


def _validation_metric_evidence(
    *, runtime_run_id: str = "run-v11", evidence_id: str = "ev-metric"
):
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        status=EvidenceStatus.SUCCESS,
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
        summary="committed metric evidence",
        runtime_run_id=runtime_run_id,
    )


def _partial_validation_kwargs(
    *,
    candidate_supporting: tuple[str, ...] = ("ev-log", "ev-metric"),
    run_evidence: tuple[EvidenceItem, ...] | None = None,
    failed_check: CausalCheckName | None = None,
) -> dict:
    """spec §9.2 合法 partial 的基准输入；调用方只覆盖被测差异。"""
    evidence = (
        list(run_evidence)
        if run_evidence is not None
        else [_validation_evidence(), _validation_metric_evidence()]
    )
    checks = [
        CausalCheck(
            name=name,
            status=(
                CausalCheckStatus.FAIL if name == failed_check else CausalCheckStatus.PASS
            ),
            summary="supported",
            evidence_ids=["ev-log"],
        )
        for name in CausalCheckName
    ]
    candidate = RootCauseCandidate(
        id="candidate-1",
        summary="candidate",
        rank=1,
        confidence=0.7,
        supporting_evidence_ids=list(candidate_supporting),
    )
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=checks,
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
    return {
        "investigation_id": "inv-1",
        "runtime_run_id": "run-v11",
        "findings": [],
        "candidates": [candidate],
        "review": review,
        "executions": executions,
        "evidence": evidence,
        "status": DiagnosticStatus.PARTIAL,
    }


def test_v11_partial_rejects_candidate_without_sufficient_independent_evidence():
    """spec §9.2：被采纳候选的支撑证据不足两条独立 evidence item 时不得 partial。"""
    with pytest.raises(Exception, match="partial_candidate_evidence"):
        validate_v11_result(**_partial_validation_kwargs(candidate_supporting=("ev-log",)))


def test_v11_partial_rejects_candidate_with_failed_causal_check():
    """spec §9.2：被采纳候选的任何 causal check 为 FAIL 时不得 partial。"""
    with pytest.raises(Exception, match="partial_candidate_failed_check"):
        validate_v11_result(
            **_partial_validation_kwargs(failed_check=CausalCheckName.TEMPORAL)
        )


def test_v11_partial_requires_two_provider_types_when_two_providers_succeeded():
    """spec §9.2：两种 Provider 均成功时候选支撑证据必须覆盖两种类型。"""
    with pytest.raises(Exception, match="partial_candidate_provider_types"):
        validate_v11_result(
            **_partial_validation_kwargs(
                candidate_supporting=("ev-log", "ev-log-2"),
                run_evidence=(
                    _validation_evidence(),
                    _validation_evidence(evidence_id="ev-log-2"),
                    _validation_metric_evidence(),
                ),
            )
        )


def test_v11_partial_without_round_two_task_is_valid():
    """spec §9.2：合法 partial 不要求 round-2 supplemental task linkage。"""
    validate_v11_result(**_partial_validation_kwargs())


def test_v11_partial_allows_single_successful_provider_type():
    """只有一种 Provider 成功时，不强制候选覆盖两种 Provider 类型。"""
    validate_v11_result(
        **_partial_validation_kwargs(
            candidate_supporting=("ev-log", "ev-log-2"),
            run_evidence=(
                _validation_evidence(),
                _validation_evidence(evidence_id="ev-log-2"),
            ),
        )
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


def test_v11_validator_rejects_whitespace_controls_in_nested_assessment_text():
    evidence = _validation_evidence()
    candidate = _validation_candidate()
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=_validation_checks(),
        summary="assessment\twith control",
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


def _skipped_evidence(*, runtime_run_id: str = "run-v11") -> EvidenceItem:
    return _validation_evidence(runtime_run_id=runtime_run_id).model_copy(
        update={"id": "ev-skipped", "status": EvidenceStatus.SKIPPED}
    )


def _investigator_finding(
    evidence_id: str, *, finding_type: AgentFindingType = AgentFindingType.GAP
) -> AgentFinding:
    return AgentFinding(
        investigation_id="inv-1",
        agent_name=FindingActor.INVESTIGATOR,
        agent_instance_id="inst-1",
        task_id="task-1",
        runtime_run_id="run-v11",
        finding_type=finding_type,
        summary="finding",
        confidence=0.4,
        evidence_ids=[evidence_id],
        gaps=["related alerts unavailable"]
        if finding_type == AgentFindingType.GAP
        else [],
    )


def test_v11_gap_finding_may_cite_committed_failed_evidence():
    # spec §7.2 只约束非 gap finding 与未提交输出：GAP 的语义是证据缺失，
    # 引用同 run 已提交的 skipped/failed 证据正是其正确出处（与准入层
    # _finding_from_draft 同一契约）。
    evidence = _skipped_evidence()
    finding = _investigator_finding(evidence.id)

    validate_v11_result(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        findings=[finding],
        candidates=[],
        review=None,
        evidence=[evidence],
    )


def test_v11_non_gap_finding_still_rejects_committed_failed_evidence():
    evidence = _skipped_evidence()
    finding = _investigator_finding(evidence.id, finding_type=AgentFindingType.SIGNAL)

    with pytest.raises(Exception, match="finding_evidence_reference"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[finding],
            candidates=[],
            review=None,
            evidence=[evidence],
        )


def test_v11_gap_finding_rejects_uncommitted_evidence():
    finding = _investigator_finding("ev-missing")

    with pytest.raises(Exception, match="finding_evidence_reference"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[finding],
            candidates=[],
            review=None,
            evidence=[],
        )


def test_v11_gap_finding_rejects_failed_evidence_from_another_run():
    evidence = _skipped_evidence(runtime_run_id="run-other")
    finding = _investigator_finding(evidence.id)

    with pytest.raises(Exception, match="finding_evidence_reference"):
        validate_v11_result(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            findings=[finding],
            candidates=[],
            review=None,
            evidence=[evidence],
        )
