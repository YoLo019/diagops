import warnings
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CausalCheck,
    CausalCheckName,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadAction,
    LeadDecision,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import (
    AuthorityMode,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionContractVersion,
    ExecutionStepKind,
    ModelProvider,
    MultiAgentRunSummary,
)
from backend.domain.react_trace import ReActTraceStep
from backend.domain.reports import IncidentReport
from backend.domain.runtime import RuntimeRun, RuntimeRunKind, RuntimeRunReason, RuntimeRunStatus


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


def _run() -> RuntimeRun:
    return RuntimeRun(
        id="run-v11",
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.LIVE,
        strategy="adaptive",
        run_reason=RuntimeRunReason.INITIAL,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        prompt_version="v11-test",
        tool_budget=8,
        token_budget=1000,
        timeout_seconds=120.0,
        execution_contract_version=ExecutionContractVersion.V11,
        authority_mode=AuthorityMode.AGENT,
        execution_contract=_contract(),
    )


def _check(name: CausalCheckName, status: CausalCheckStatus = CausalCheckStatus.PASS):
    return CausalCheck(
        name=name,
        status=status,
        summary="Scoped evidence supports this check.",
        evidence_ids=[] if status == CausalCheckStatus.UNKNOWN else ["ev-1"],
        gap="need a scoped signal" if status == CausalCheckStatus.UNKNOWN else None,
    )


def _assessment(candidate_id: str = "candidate-1") -> CriticAssessment:
    return CriticAssessment(
        candidate_id=candidate_id,
        verdict=CriticVerdict.ACCEPT,
        checks=[_check(name) for name in CausalCheckName],
        supporting_evidence_ids=["ev-1"],
        summary="The candidate passes the required causal checks.",
        runtime_run_id="run-v11",
    )


def test_v11_contract_enums_and_runtime_identity_are_bounded() -> None:
    assert FindingActor.INVESTIGATOR == "InvestigatorAgent"
    assert LeadAction.CONCLUDE == "conclude"
    assert CriticVerdict.NEEDS_EVIDENCE == "needs_evidence"
    assert DiagnosticStatus.INCONCLUSIVE == "inconclusive"
    assert _run().status == RuntimeRunStatus.CREATED


def test_v11_run_requires_a_frozen_token_ceiling() -> None:
    with pytest.raises(ValueError, match="token_budget"):
        RuntimeRun(
            id="run-v11-no-token-ceiling",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy="adaptive",
            run_reason=RuntimeRunReason.INITIAL,
            model_provider=ModelProvider.OPENAI,
            model_name="gpt-test",
            prompt_version="v11-test",
            tool_budget=8,
            token_budget=None,
            timeout_seconds=120.0,
            execution_contract_version=ExecutionContractVersion.V11,
            authority_mode=AuthorityMode.AGENT,
            execution_contract={**_contract(), "token_budget": None},
        )


def test_v11_lead_decision_rejects_unbounded_or_invalid_action() -> None:
    with pytest.raises(ValidationError):
        LeadDecision(action=LeadAction.INVESTIGATE, summary="x", task_ids=[])

    with pytest.raises(ValidationError):
        LeadDecision(
            action=LeadAction.INCONCLUSIVE,
            summary="No safe conclusion.",
            task_ids=["task-1"],
            stop_reason="evidence ended",
        )


def test_v11_plan_and_summary_require_runtime_owner() -> None:
    with pytest.raises(ValidationError, match="runtime_run_id"):
        DiagnosisPlan(
            investigation_id="inv-1",
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="No safe conclusion.",
                stop_reason="evidence ended",
            ),
        )

    with pytest.raises(ValidationError, match="runtime_run_id"):
        MultiAgentRunSummary(
            status="completed",
            authority_mode=AuthorityMode.AGENT,
        )


def test_v11_result_validation_without_round_is_not_legacy() -> None:
    with pytest.raises(ValidationError, match="runtime_run_id"):
        AgentExecution(
            task_id="task-result-validation-no-round",
            agent_name="CoordinatorAgent",
            step_kind=ExecutionStepKind.RESULT_VALIDATION,
            runtime_run_id=None,
        )


def test_v11_plan_rejects_final_conclude_and_requires_owned_generic_task() -> None:
    task = DiagnosisTask(
        title="Trace the first failure",
        description="Find the earliest causal boundary.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name=FindingActor.INVESTIGATOR,
        runtime_run_id="run-v11",
        analysis_round=1,
        evidence_scope={"entity_ids": ["checkout-service"]},
    )
    with pytest.raises(ValidationError):
        DiagnosisPlan(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.CONCLUDE,
                summary="Conclude before Critic.",
                candidate_ids=["candidate-1"],
            ),
        )


def test_v11_test_task_requires_discriminator_and_bounded_tools() -> None:
    task = DiagnosisTask(
        title="Test the dependency hypothesis",
        description="Run a bounded discriminator check.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name=FindingActor.INVESTIGATOR,
        runtime_run_id="run-v11",
        analysis_round=1,
        evidence_scope={"entity_ids": ["checkout-service"]},
    )
    with pytest.raises(ValidationError, match="expected_discriminator"):
        DiagnosisPlan(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.TEST,
                summary="Test the leading explanation.",
                task_ids=[task.id],
            ),
        )

    task.expected_discriminator = "dependency timeout disappears"
    task.tool_names = [f"tool-{index}" for index in range(10)]
    with pytest.raises(ValidationError, match="tool_names"):
        DiagnosisPlan(
            investigation_id="inv-1",
            runtime_run_id="run-v11",
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.TEST,
                summary="Test the leading explanation.",
                task_ids=[task.id],
            ),
        )


def test_v11_investigator_round_two_can_add_finding_without_revision() -> None:
    finding = AgentFinding(
        investigation_id="inv-1",
        agent_name=FindingActor.INVESTIGATOR,
        agent_instance_id="investigator-1",
        finding_type=AgentFindingType.SIGNAL,
        summary="Supplemental trace evidence",
        confidence=0.8,
        evidence_ids=["ev-1"],
        task_id="task-round-2",
        runtime_run_id="run-v11",
        critic_assessment_id="assessment-1",
        analysis_round=2,
        affected_entity="checkout-service",
        failure_mechanism="dependency timeout",
    )
    assert finding.revises_finding_id is None


def test_legacy_round_two_still_requires_revision() -> None:
    with pytest.raises(ValidationError, match="revises_finding_id"):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=FindingActor.LOG,
            finding_type=AgentFindingType.SIGNAL,
            summary="Legacy revision",
            confidence=0.8,
            evidence_ids=["ev-1"],
            analysis_round=2,
        )


def test_legacy_agent_name_is_normalized_to_finding_actor() -> None:
    finding = AgentFinding(
        investigation_id="inv-1",
        agent_name=AgentName.METRIC,
        finding_type=AgentFindingType.GAP,
        summary="legacy metric gap",
        confidence=0.5,
        analysis_round=1,
    )

    assert type(finding.agent_name) is FindingActor

    forged_legacy_value = finding.model_copy(update={"agent_name": AgentName.METRIC})
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        forged_legacy_value.model_dump(mode="json")
    assert not any("Pydantic serializer warnings" in str(item.message) for item in captured)


def test_critic_requires_exactly_seven_named_checks_and_valid_unknown_gap() -> None:
    assessment = _assessment()
    assert len(assessment.checks) == 7
    assert {item.name for item in assessment.checks} == set(CausalCheckName)

    with pytest.raises(ValidationError):
        CriticAssessment(
            candidate_id="candidate-1",
            verdict=CriticVerdict.ACCEPT,
            checks=assessment.checks[:-1],
            summary="Incomplete checks.",
        )

    with pytest.raises(ValidationError):
        CausalCheck(
            name=CausalCheckName.TEMPORAL,
            status=CausalCheckStatus.UNKNOWN,
            summary="unknown",
            gap=None,
        )


def test_review_uses_lead_accepted_candidates_as_v11_authority() -> None:
    candidate = RootCauseCandidate(
        id="candidate-1",
        cause_type=None,
        affected_entity="checkout-service",
        failure_mechanism="dependency timeout",
        summary="Checkout dependency timed out.",
        rank=1,
        confidence=0.9,
        supporting_evidence_ids=["ev-1"],
        onset_window_start=datetime(2026, 8, 5, 0, tzinfo=UTC),
        onset_window_end=datetime(2026, 8, 5, 1, tzinfo=UTC),
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        authority_mode=AuthorityMode.AGENT,
        candidates=[candidate],
        critic_assessments=[_assessment()],
        lead_decision=LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="Critic accepted the candidate.",
            candidate_ids=[candidate.id],
            evidence_ids=["ev-1"],
        ),
        diagnostic_status=DiagnosticStatus.COMPLETE,
    )
    assert review.authoritative_candidate_ids == [candidate.id]


def test_v11_artifacts_require_one_runtime_owner() -> None:
    evidence = EvidenceItem(
        id="ev-1",
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        summary="error rate increased",
        runtime_run_id="run-v11",
    )
    report = IncidentReport(
        investigation_id="inv-1",
        runtime_run_id="run-v11",
        summary="bounded report",
        markdown="bounded report",
        diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
        authority_mode=AuthorityMode.AGENT,
    )
    assert evidence.runtime_run_id == report.runtime_run_id

    with pytest.raises(ValidationError):
        IncidentReport(
            investigation_id="inv-1",
            summary="missing owner",
            markdown="missing owner",
            diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
            authority_mode=AuthorityMode.AGENT,
        )


def test_v11_react_step_accepts_structured_summary_but_not_assistant_text() -> None:
    step = ReActTraceStep(
        step_number=1,
        runtime_run_id="run-v11",
        structured_summary={"decision": "query metrics"},
    )
    assert step.structured_summary == {"decision": "query metrics"}

    with pytest.raises(ValidationError):
        ReActTraceStep(
            step_number=1,
            runtime_run_id="run-v11",
            assistant_text="private reasoning",
            structured_summary={"decision": "query metrics"},
        )
