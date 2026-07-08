import logging
from datetime import UTC, datetime

import pytest

from backend.config.settings import AppSettings, LlmSettings, StorageSettings
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.execution_engine import DiagnosisExecutionEngine
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.diagnosis.react_agent import ReActInvestigationAgent, ReActLlmResponse
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.agent_findings import AgentName
from backend.domain.evidence import EvidenceProvider
from backend.domain.hypotheses import CauseType
from backend.domain.react_trace import ReActTraceStatus
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.providers.results import ProviderResult
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.container import AppContainer
from backend.services.incident_cases import load_incident_case
from backend.tools.provider_tools import build_provider_tool_registry


def build_v2_orchestrator(
    repository=None,
    providers=None,
    analyzer=None,
    report_generator=None,
):
    repository = repository or InMemoryInvestigationRepository()
    providers = providers or build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=analyzer or RcaAnalyzer(),
        report_generator=report_generator or ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        action_planner=ActionPlanner(),
    )


def test_orchestrator_creates_investigation_with_report():
    repository = InMemoryInvestigationRepository()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=build_mock_provider_registry(),
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
    )

    investigation = orchestrator.run(load_incident_case("deployment_regression"))

    stored = repository.get(investigation.id)
    assert stored.id == investigation.id
    assert stored.report is not None
    assert stored.hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert "最可能根因" in stored.report.markdown


def test_orchestrator_persists_completed_record_with_actions_and_verifications():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert saved.status == InvestigationStatus.COMPLETED
    assert saved.evidence
    assert saved.hypotheses
    assert saved.report is not None
    assert saved.actions
    assert saved.verification_suggestions
    assert saved.provider_results
    assert saved.specialist_results
    assert saved.completed_at is not None


def test_orchestrator_records_v4_plan_tasks_executions_and_tool_calls():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)

    record = orchestrator.run(load_incident_case("deployment_regression"))

    plan = repository.get_plan(record.id)
    tasks = repository.list_tasks(record.id)
    executions = repository.list_executions(record.id)
    tool_calls = repository.list_tool_calls(record.id)

    assert plan is not None
    assert plan.investigation_id == record.id
    assert tasks == plan.tasks
    assert tasks
    assert len(executions) == len(tasks)
    assert tool_calls
    assert {call.task_id for call in tool_calls} <= {task.id for task in tasks}


def test_orchestrator_records_v4_without_collecting_provider_twice():
    class CountingProvider:
        provider = EvidenceProvider.LOG

        def __init__(self):
            self.calls = 0

        def collect(self, event):
            self.calls += 1
            return ProviderResult(provider=self.provider)

    provider = CountingProvider()
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(
        repository=repository,
        providers=ProviderRegistry([provider]),
    )

    orchestrator.run(load_incident_case("deployment_regression"))

    assert provider.calls == 1


def test_orchestrator_records_v5_findings_and_coordination_review():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)

    record = orchestrator.run(load_incident_case("deployment_regression"))

    findings = repository.list_agent_findings(record.id)
    review = repository.get_coordination_review(record.id)

    assert {finding.agent_name for finding in findings} >= {
        AgentName.LOG,
        AgentName.METRIC,
        AgentName.DEPLOYMENT,
    }
    assert review is not None
    assert review.candidates
    assert review.candidates[0].supporting_evidence_ids


class OneShotReactLlm:
    def generate(self, messages, tools):
        return ReActLlmResponse(content="Read-only ReAct review complete.")


def test_orchestrator_records_v6_react_trace_when_agent_is_configured():
    repository = InMemoryInvestigationRepository()
    providers = build_mock_provider_registry()
    tool_registry = build_provider_tool_registry(providers)
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        execution_engine=DiagnosisExecutionEngine(
            repository=repository,
            tool_registry=tool_registry,
        ),
        react_agent=ReActInvestigationAgent(
            llm=OneShotReactLlm(),
            tool_registry=tool_registry,
            max_steps=1,
        ),
    )

    record = orchestrator.run(load_incident_case("deployment_regression"))
    trace = repository.get_react_trace(record.id)

    assert trace is not None
    assert trace.status == ReActTraceStatus.COMPLETED
    assert trace.final_answer == "Read-only ReAct review complete."


def test_orchestrator_persists_failed_record_when_report_generation_fails():
    class BrokenReportGenerator:
        def generate(self, *args, **kwargs):
            raise RuntimeError("markdown exploded")

    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(
        repository=repository,
        report_generator=BrokenReportGenerator(),
    )
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert record.status == InvestigationStatus.FAILED
    assert saved.status == InvestigationStatus.FAILED
    assert saved.failure_reason == "markdown exploded"
    assert saved.evidence
    assert saved.hypotheses


def test_orchestrator_preserves_context_when_action_planner_fails():
    class BrokenActionPlanner:
        def plan(self, *args, **kwargs):
            raise RuntimeError("planner exploded")

    repository = InMemoryInvestigationRepository()
    providers = build_mock_provider_registry()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        action_planner=BrokenActionPlanner(),
    )
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert saved.status == InvestigationStatus.FAILED
    assert saved.failure_reason == "planner exploded"
    assert saved.evidence
    assert saved.hypotheses
    assert saved.provider_results
    assert saved.specialist_results
    assert saved.actions == []


def test_repository_rejects_unknown_investigation_id():
    repository = InMemoryInvestigationRepository()

    with pytest.raises(ValueError, match="Unknown investigation: inv-missing"):
        repository.get("inv-missing")


def test_repository_lists_records_by_created_at_descending():
    event = load_incident_case("deployment_regression")
    repository = InMemoryInvestigationRepository()
    older = InvestigationRecord(
        id="inv-older",
        event=event,
        status=InvestigationStatus.COMPLETED,
        created_at=datetime(2026, 7, 3, 8, 0, tzinfo=UTC),
    )
    newer = InvestigationRecord(
        id="inv-newer",
        event=event,
        status=InvestigationStatus.COMPLETED,
        created_at=datetime(2026, 7, 3, 9, 0, tzinfo=UTC),
    )

    repository.save(older)
    repository.save(newer)

    assert repository.list() == [newer, older]


def test_repository_updates_status_and_failure_reason():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    record = repository.save(InvestigationRecord(event=event))

    updated = repository.update_status(
        record.id,
        InvestigationStatus.FAILED,
        failure_reason="report generation failed",
    )

    assert updated.status == InvestigationStatus.FAILED
    assert updated.failure_reason == "report generation failed"
    assert updated.updated_at >= record.created_at


def test_repository_updates_action_and_verification_status():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Deployment regression likely.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-1"],
    )
    verification = VerificationSuggestion(
        title="Check 5xx",
        description="Confirm error rate recovery.",
        expected_signal="5xx below 1%",
    )
    record = repository.save(
        InvestigationRecord(
            event=event,
            actions=[action],
            verification_suggestions=[verification],
        )
    )

    updated_action = repository.update_action_status(
        record.id,
        action.id,
        status="approved",
        note="owner approved",
    )
    updated_verification = repository.update_verification_status(
        record.id,
        verification.id,
        status="passed",
        result_note="5xx is normal",
    )

    assert updated_action.status == "approved"
    assert updated_action.note == "owner approved"
    assert updated_verification.status == "passed"
    assert updated_verification.result_note == "5xx is normal"


def test_orchestrator_logs_investigation_lifecycle(caplog):
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    with caplog.at_level(logging.INFO):
        record = orchestrator.run(event)

    messages = "\n".join(item.message for item in caplog.records)
    assert record.id in messages
    assert "investigation started" in messages
    assert "investigation completed" in messages


def test_orchestrator_does_not_call_analyst_when_not_configured():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    orchestrator.llm_analyst = None

    record = orchestrator.run(load_incident_case("deployment_regression"))

    assert record.status == InvestigationStatus.COMPLETED
    assert record.llm_analysis is None


def test_container_disables_analyst_when_llm_config_disabled():
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            llm=LlmSettings(enabled=False),
        )
    )

    record = container.orchestrator.run(load_incident_case("deployment_regression"))

    assert container.orchestrator.llm_analyst is None
    assert record.llm_analysis is None


def test_orchestrator_persists_llm_analysis_when_analyst_configured():
    class StubAnalyst:
        def analyze(self, *, investigation_id, evidence, hypotheses):
            from backend.domain.llm_analysis import LLMAnalysis

            return LLMAnalysis.create(
                investigation_id=investigation_id,
                existing_evidence_ids={item.id for item in evidence},
                summary="stub summary",
                referenced_evidence_ids=[evidence[0].id],
            )

    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    orchestrator.llm_analyst = StubAnalyst()

    record = orchestrator.run(load_incident_case("deployment_regression"))

    assert record.llm_analysis is not None
    assert repository.get(record.id).llm_analysis == record.llm_analysis
