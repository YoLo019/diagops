import asyncio
from datetime import UTC, datetime

import pytest

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.actions import VerificationStatus
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseCandidate,
)
from backend.domain.agent_plan import LeadDecision
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.human_transitions import HumanStateConflict, validate_verification_transition
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.domain.runtime import RuntimePhase, RuntimeResumeState
from backend.domain.v11_contracts import validate_v11_report_projection
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import PhaseInput

RUN_ID = "run-v11-product"


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-api",
        environment="prod",
        severity=Severity.CRITICAL,
        title="checkout errors",
        description="checkout requests are failing",
        started_at=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
    )


def _evidence() -> EvidenceItem:
    return EvidenceItem(
        id="ev-deploy",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime(2026, 8, 5, 0, 1, tzinfo=UTC),
        summary="checkout-api deployed version 2.4.0",
        runtime_run_id=RUN_ID,
    )


def _candidate() -> RootCauseCandidate:
    return RootCauseCandidate(
        id="candidate-deploy",
        summary="checkout-api deployment introduced the failing version",
        rank=1,
        confidence=0.9,
        affected_entity="checkout-api",
        failure_mechanism="deployment introduced incompatible request handling",
        supporting_evidence_ids=["ev-deploy"],
    )


def _review(candidate: RootCauseCandidate, *, inconclusive: bool = False) -> CoordinationReview:
    assessment = CriticAssessment(
        id="assessment-deploy",
        candidate_id=candidate.id,
        verdict=(
            CriticVerdict.NEEDS_EVIDENCE
            if inconclusive
            else CriticVerdict.ACCEPT
        ),
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="check requires bounded evidence",
                gap="additional evidence is required",
            )
            for name in CausalCheckName
        ],
        summary="critic reviewed the candidate",
        gap="additional evidence is required" if inconclusive else None,
        supplemental_task_ids=["task-deploy-follow-up"] if inconclusive else [],
        runtime_run_id=RUN_ID,
    )
    decision = (
        LeadDecision(
            action=LeadAction.INCONCLUSIVE,
            summary="evidence is insufficient",
            stop_reason="insufficient_evidence",
        )
        if inconclusive
        else LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="the candidate is supported",
            candidate_ids=[candidate.id],
            evidence_ids=["ev-deploy"],
        )
    )
    return CoordinationReview(
        investigation_id="inv-v11-product",
        candidates=[candidate],
        critic_assessments=[assessment],
        lead_decision=decision,
        diagnostic_status=(
            DiagnosticStatus.INCONCLUSIVE
            if inconclusive
            else DiagnosticStatus.COMPLETE
        ),
        runtime_run_id=RUN_ID,
        authority_mode=AuthorityMode.AGENT,
        summary="V11 review summary",
    )


def _run_summary(status: DiagnosticStatus) -> MultiAgentRunSummary:
    return MultiAgentRunSummary(
        status=MultiAgentRunStatus.COMPLETED,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        diagnostic_status=status,
        authority_mode=AuthorityMode.AGENT,
        runtime_run_id=RUN_ID,
        total_input_tokens=13,
        total_output_tokens=21,
        elapsed_time_ms=34,
    )


def test_v11_report_projects_candidate_led_diagnoses_without_hypotheses():
    candidate = _candidate().model_copy(update={"rationale": "private chain of thought"})
    report = ReportGenerator().generate(
        "inv-v11-product",
        _event(),
        [_evidence()],
        [],
        coordination_review=_review(candidate),
        multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
    )

    assert report.hypotheses == []
    assert report.diagnoses[0].id == candidate.id
    assert report.diagnoses[0].rationale == "[内部推理内容已省略]"
    assert report.alternatives == []
    assert report.authority_mode == AuthorityMode.AGENT
    assert report.diagnostic_status == DiagnosticStatus.COMPLETE
    assert report.runtime_run_id == RUN_ID
    assert "deployment introduced incompatible request handling" in report.markdown
    assert "private chain of thought" not in report.markdown
    assert "private chain of thought" not in report.model_dump_json()


def test_tentative_diagnosis_remains_visible_with_redacted_uncertainty():
    from backend.domain.agent_findings import FinalDiagnosisDecision

    review = _review(_candidate())
    review.final_decision = FinalDiagnosisDecision(
        actor="critic", **review.lead_decision.model_dump(
            include={"action", "candidate_ids", "evidence_ids", "summary", "stop_reason"}
        ), uncertainty="Missing capacity evidence; token=private-secret",
    )
    review.diagnostic_status = DiagnosticStatus.PARTIAL
    review.run_status = MultiAgentRunStatus.PARTIAL
    run = _run_summary(DiagnosticStatus.PARTIAL).model_copy(
        update={"status": MultiAgentRunStatus.PARTIAL},
    )
    report = ReportGenerator().generate(
        "inv-v11-product", _event(), [_evidence()], [],
        coordination_review=review, multi_agent_run=run,
    )
    assert report.diagnoses[0].id == review.candidates[0].id
    assert report.diagnostic_status == DiagnosticStatus.PARTIAL
    assert "最可能原因（暂定" in report.markdown
    assert "Missing capacity evidence" in report.markdown
    assert "private-secret" not in report.model_dump_json()


def test_v11_report_projection_rejects_tampered_lead_candidate_reference():
    candidate = _candidate()
    review = _review(candidate)
    report = ReportGenerator().generate(
        "inv-v11-product",
        _event(),
        [_evidence()],
        [],
        coordination_review=review,
        multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
    )
    tampered = report.model_copy(
        update={
            "diagnoses": [candidate.model_copy(update={"id": "candidate-foreign"})]
        }
    )

    with pytest.raises(ValueError, match="Lead candidate references"):
        validate_v11_report_projection(review, tampered)


@pytest.mark.parametrize(
    "run_update",
    [
        {"diagnostic_status": DiagnosticStatus.PARTIAL},
        {"status": MultiAgentRunStatus.PARTIAL},
    ],
)
def test_v11_report_rejects_inconsistent_review_run_projection(run_update):
    review = _review(_candidate())
    run = _run_summary(DiagnosticStatus.COMPLETE).model_copy(update=run_update)

    with pytest.raises(ValueError, match="V11 review and run"):
        ReportGenerator().generate(
            "inv-v11-product",
            _event(),
            [_evidence()],
            [],
            coordination_review=review,
            multi_agent_run=run,
        )


def test_v11_markdown_renders_actual_findings_tasks_rounds_and_lead_decision():
    finding = AgentFinding(
        id="finding-log-actual",
        investigation_id="inv-v11-product",
        agent_name=FindingActor.LOG,
        agent_instance_id="log-instance-7",
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="deployment evidence explains request failure",
        rationale="bounded public rationale",
        confidence=0.8,
        evidence_ids=["ev-deploy"],
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        task_id="task-log-actual",
        runtime_run_id=RUN_ID,
        analysis_round=1,
    )
    report = ReportGenerator().generate(
        "inv-v11-product",
        _event(),
        [_evidence()],
        [],
        coordination_review=_review(_candidate()),
        multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
        agent_findings=[finding],
    )

    assert "finding-log-actual" in report.markdown
    assert "log-instance-7" in report.markdown
    assert "task-log-actual" in report.markdown
    assert "analysis round" in report.markdown
    assert "conclude" in report.markdown
    assert "LeadAgent / InvestigatorAgent / CriticAgent" not in report.markdown
    assert "见 Critic 评估" not in report.markdown


def test_v11_action_planner_binds_read_only_recommendations_to_candidates():
    candidate = _candidate()
    actions, verifications = ActionPlanner().plan_v11(
        _event(),
        [_evidence()],
        _review(candidate),
        _run_summary(DiagnosticStatus.COMPLETE),
    )

    assert actions
    assert all(action.related_candidate_ids == [candidate.id] for action in actions)
    assert all(action.runtime_run_id == RUN_ID for action in actions)
    assert all(action.risk_level.value == "read_only" for action in actions)
    assert all(not suggestion.related_cause_types for suggestion in verifications)
    assert all(suggestion.related_candidate_ids == [candidate.id] for suggestion in verifications)


def test_v11_generated_recommendations_are_safe_before_persistence():
    from backend.safety.redaction import assert_safe_value

    candidate = _candidate().model_copy(update={
        "failure_mechanism": "Slow requests on the error path: internal-value",
    })
    actions, verifications = ActionPlanner().plan_v11(
        _event(), [_evidence()], _review(candidate), _run_summary(DiagnosticStatus.COMPLETE),
    )
    for item in [*actions, *verifications]:
        assert_safe_value(item.model_dump(mode="json"))
    assert "internal-value" not in actions[0].description
    assert actions[0].supporting_evidence_ids == candidate.supporting_evidence_ids


def test_v11_inconclusive_report_does_not_activate_diagnosis_or_actions():
    candidate = _candidate()
    review = _review(candidate, inconclusive=True)
    report = ReportGenerator().generate(
        "inv-v11-product",
        _event(),
        [_evidence()],
        [],
        coordination_review=review,
        multi_agent_run=_run_summary(DiagnosticStatus.INCONCLUSIVE),
    )
    actions, verifications = ActionPlanner().plan_v11(
        _event(),
        [_evidence()],
        review,
        _run_summary(DiagnosticStatus.INCONCLUSIVE),
    )

    assert report.diagnoses == []
    assert report.alternatives == []
    assert actions == []
    assert verifications == []
    assert "not_activated" in report.markdown
    assert "task-deploy-follow-up" in report.markdown


def test_v11_human_verification_rejects_candidate_from_another_projection():
    candidate = _candidate()
    verification = ActionPlanner().plan_v11(
        _event(), [_evidence()], _review(candidate), _run_summary(DiagnosticStatus.COMPLETE)
    )[1][0]
    record = InvestigationRecord(
        id="inv-v11-product",
        event=_event(),
        strategy=InvestigationStrategy.ADAPTIVE,
        status=InvestigationStatus.COMPLETED,
        evidence=[_evidence()],
        verification_suggestions=[verification],
        report=IncidentReport(
            investigation_id="inv-v11-product",
            summary=candidate.summary,
            markdown="safe report",
            diagnoses=[candidate],
            authority_mode=AuthorityMode.AGENT,
            runtime_run_id=RUN_ID,
        ),
        active_runtime_run_id=RUN_ID,
    )

    with pytest.raises(HumanStateConflict, match="verification reference"):
        validate_verification_transition(
            record,
            verification.id,
            VerificationStatus.SKIPPED,
            None,
            [],
            [],
            [],
            ["candidate-from-another-run"],
        )


def test_v11_human_verification_cannot_replace_persisted_candidate_refs():
    candidate = _candidate()
    other = _candidate().model_copy(
        update={"id": "candidate-other", "rank": 2, "summary": "another candidate"}
    )
    review = _review(candidate)
    actions, verifications = ActionPlanner().plan_v11(
        _event(), [_evidence()], review, _run_summary(DiagnosticStatus.COMPLETE)
    )
    record = InvestigationRecord(
        id="inv-v11-product",
        event=_event(),
        strategy=InvestigationStrategy.ADAPTIVE,
        status=InvestigationStatus.COMPLETED,
        evidence=[_evidence()],
        actions=actions,
        verification_suggestions=verifications,
        report=IncidentReport(
            investigation_id="inv-v11-product",
            summary=candidate.summary,
            markdown="safe report",
            diagnoses=[candidate],
            alternatives=[other],
            authority_mode=AuthorityMode.AGENT,
            runtime_run_id=RUN_ID,
        ),
        active_runtime_run_id=RUN_ID,
    )

    with pytest.raises(HumanStateConflict, match="verification reference"):
        validate_verification_transition(
            record,
            verifications[0].id,
            VerificationStatus.SKIPPED,
            None,
            [],
            [],
            [],
            [other.id],
        )


def test_v11_report_phase_uses_candidate_product_path(monkeypatch):
    """V11 报告 phase 必须通过显式 candidate contract 生成产品投影。"""
    from backend.db.repositories import InMemoryInvestigationRepository

    repository = InMemoryInvestigationRepository()
    candidate = _candidate()
    review = _review(candidate)
    record = repository.save(
        InvestigationRecord(
            id="inv-v11-product",
            event=_event(),
            strategy=InvestigationStrategy.ADAPTIVE,
            status=InvestigationStatus.COMPLETED,
            evidence=[_evidence()],
            multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
            active_runtime_run_id=RUN_ID,
        )
    )
    repository.save_coordination_review(review)

    class ForbiddenLegacyPath:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("V11 report phase called legacy action planner")

        def generate(self, *_args, **_kwargs):
            raise AssertionError("V11 report phase called legacy report generator")

    planner = ActionPlanner()
    generator = ReportGenerator()
    monkeypatch.setattr(planner, "plan", ForbiddenLegacyPath().plan)
    monkeypatch.setattr(generator, "generate", ForbiddenLegacyPath().generate)
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=ProviderRegistry([]),
        analyzer=RcaAnalyzer(),
        report_generator=generator,
        coordinator=DiagnosisCoordinator(ProviderRegistry([])),
        action_planner=planner,
        agents_runtime=None,
        v11_runtime=type(
            "V11Owner",
            (),
            {"runtime_run_id": RUN_ID, "remaining_token_budget": 10_000},
        )(),
        default_strategy=InvestigationStrategy.ADAPTIVE,
    )
    executor = DiagnosisPhaseExecutor(orchestrator)
    executor._is_durable_session = True
    executor._execution_contract_version = "v11"

    output = asyncio.run(
        executor.execute_phase(
            PhaseInput(
                run_id=RUN_ID,
                attempt_id="attempt-report",
                phase=RuntimePhase.REPORT_GENERATION,
                resume_state=RuntimeResumeState(),
                investigation_id=record.id,
                strategy=record.strategy,
                execution_contract_version="v11",
                model_provider=ModelProvider.OPENAI,
                model_name="gpt-test",
                prompt_version="v11",
                tool_budget=8,
                token_budget=10_000,
                timeout_seconds=120,
            )
        )
    )

    projected = output.business_mutation.investigation
    assert projected is not None
    assert projected.report is not None
    assert projected.report.diagnoses[0].id == candidate.id
    assert projected.actions
    assert projected.actions[0].related_candidate_ids == [candidate.id]
