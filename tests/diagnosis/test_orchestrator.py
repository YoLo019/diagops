import asyncio
import logging
from datetime import UTC, datetime

import pytest

from backend.config.settings import (
    AgentsSettings,
    AppSettings,
    LlmSettings,
    StorageSettings,
)
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import AgentsRcaRuntimeResult
from backend.diagnosis.coordination_review import build_hybrid_coordination_review
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
from backend.domain.agent_findings import AgentFindingType, AgentName
from backend.domain.agent_plan import AgentExecutionStatus, DiagnosisTaskStatus
from backend.domain.evidence import EvidenceProvider
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
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
    agents_runtime=None,
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
        agents_runtime=agents_runtime,
    )


def sdk_result(repository, investigation_id, status, *, review=True):
    failed = AgentsRcaRuntimeResult.failed(investigation_id, "stub failure")
    record_status = (
        DiagnosisTaskStatus.FAILED
        if status == MultiAgentRunStatus.PARTIAL
        else DiagnosisTaskStatus.COMPLETED
    )
    task = failed.tasks[0].model_copy(
        update={
            "id": f"task-sdk-{status}",
            "status": record_status,
            "analysis_round": 1,
        }
    )
    execution = failed.executions[0].model_copy(
        update={
            "id": f"exec-sdk-{status}",
            "task_id": task.id,
            "status": AgentExecutionStatus(record_status.value),
            "analysis_round": 1,
            "error_message": None,
        }
    )
    tasks = [task]
    executions = [execution]
    if status == MultiAgentRunStatus.PARTIAL:
        completed_task = task.model_copy(
            update={
                "id": "task-sdk-partial-completed",
                "agent_name": "LogAgent",
                "status": DiagnosisTaskStatus.COMPLETED,
            }
        )
        tasks.append(completed_task)
        executions.append(
            execution.model_copy(
                update={
                    "id": "exec-sdk-partial-completed",
                    "task_id": completed_task.id,
                    "agent_name": completed_task.agent_name,
                    "status": AgentExecutionStatus.COMPLETED,
                }
            )
        )
    persisted = repository.get(investigation_id)
    finding = repository.list_agent_findings(investigation_id)[0].model_copy(
        update={
            "id": f"finding-sdk-{status}",
            "finding_type": AgentFindingType.ROOT_CAUSE,
            "related_cause_type": persisted.hypotheses[0].cause_type,
            "confidence": 0.9,
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
        }
    )
    sdk_review = build_hybrid_coordination_review(
        investigation_id,
        [finding],
        persisted.evidence,
        persisted.hypotheses,
        status,
        "SDK review",
        "",
    ).model_copy(
        update={"id": f"review-sdk-{status}"}
    )
    return AgentsRcaRuntimeResult(
        tasks=tasks,
        executions=executions,
        findings=[finding],
        review=sdk_review if review else None,
        run_summary=MultiAgentRunSummary(status=status),
    )


class StubAgentsRuntime:
    def __init__(self, repository, status=MultiAgentRunStatus.COMPLETED):
        self.repository = repository
        self.status = status
        self.calls = []

    async def run(self, *, investigation_id, event, evidence, hypotheses):
        persisted = self.repository.get(investigation_id)
        assert event == persisted.event
        assert evidence == persisted.evidence
        assert hypotheses == persisted.hypotheses
        assert self.repository.list_agent_findings(investigation_id)
        assert self.repository.get_coordination_review(investigation_id) is not None
        self.calls.append((investigation_id, event, evidence, hypotheses))
        return sdk_result(self.repository, investigation_id, self.status)


def corrupt_sdk_result(result, repository, investigation_id, case):
    task = result.tasks[0]
    execution = result.executions[0]
    finding = result.findings[0]
    if case == "duplicate_task_id":
        result.tasks.append(task.model_copy())
    elif case == "empty_tasks":
        result.tasks = []
    elif case == "empty_executions":
        result.executions = []
    elif case == "missing_execution":
        result.tasks.append(task.model_copy(update={"id": "task-sdk-unexecuted"}))
    elif case == "duplicate_per_task":
        result.executions.append(
            execution.model_copy(update={"id": "exec-sdk-duplicate-task"})
        )
    elif case == "task_layer":
        task.execution_layer = AgentExecutionLayer.CUSTOM
    elif case == "duplicate_execution_id":
        result.executions.append(execution.model_copy())
    elif case == "execution_layer":
        execution.execution_layer = AgentExecutionLayer.CUSTOM
    elif case == "execution_task":
        execution.task_id = "task-unknown"
    elif case == "execution_agent":
        execution.agent_name = "OtherAgent"
    elif case == "execution_round":
        execution.analysis_round = 2
    elif case == "execution_status":
        execution.status = AgentExecutionStatus.FAILED
    elif case == "execution_evidence":
        execution.evidence_ids = ["ev-unknown"]
    elif case == "duplicate_finding_id":
        result.findings.append(finding.model_copy())
    elif case == "finding_collision":
        finding.id = repository.list_agent_findings(investigation_id)[0].id
    elif case == "finding_layer":
        finding.execution_layer = AgentExecutionLayer.CUSTOM
    elif case == "finding_investigation":
        finding.investigation_id = "inv-wrong"
    elif case == "finding_evidence":
        finding.evidence_ids = ["ev-unknown"]
    elif case == "round2_unknown":
        finding.analysis_round = 2
        finding.revises_finding_id = "finding-unknown"
    elif case == "round2_cross_layer":
        finding.analysis_round = 2
        finding.revises_finding_id = repository.list_agent_findings(
            investigation_id
        )[0].id
    return result


def invalid_run_result(repository, investigation_id, case):
    status = (
        MultiAgentRunStatus.PARTIAL
        if case.startswith(("partial", "failed"))
        else MultiAgentRunStatus.COMPLETED
    )
    result = sdk_result(repository, investigation_id, status)
    if case == "completed_review_none":
        result.review = None
    elif case == "completed_zero_findings":
        result.findings = []
        result.review.candidates[0].supporting_finding_ids = []
    elif case == "completed_baseline_only_review":
        result.review.candidates[0].supporting_finding_ids = []
    elif case == "completed_failed_record":
        result.tasks[0].status = DiagnosisTaskStatus.FAILED
        result.executions[0].status = AgentExecutionStatus.FAILED
    elif case == "partial_review_none":
        result.review = None
    elif case == "partial_zero_findings":
        result.findings = []
        result.review.candidates[0].supporting_finding_ids = []
    elif case == "partial_all_completed":
        result.tasks[0].status = DiagnosisTaskStatus.COMPLETED
        result.executions[0].status = AgentExecutionStatus.COMPLETED
    elif case == "partial_skipped_record":
        result.tasks[0].status = DiagnosisTaskStatus.SKIPPED
        result.executions[0].status = AgentExecutionStatus.SKIPPED
    elif case == "partial_all_failed":
        for task in result.tasks:
            task.status = DiagnosisTaskStatus.FAILED
        for execution in result.executions:
            execution.status = AgentExecutionStatus.FAILED
    elif case in {"failed_no_failed_record", "failed_with_review"}:
        result.run_summary = MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED)
        result.review.run_status = MultiAgentRunStatus.FAILED
        if case == "failed_no_failed_record":
            for task in result.tasks:
                task.status = DiagnosisTaskStatus.COMPLETED
            for execution in result.executions:
                execution.status = AgentExecutionStatus.COMPLETED
            result.review = None
    elif case.startswith("failed_"):
        result.run_summary = MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED)
        result.review = None
        bad_status = case.removeprefix("failed_").removesuffix("_record")
        result.tasks[1].status = DiagnosisTaskStatus(bad_status)
        result.executions[1].status = AgentExecutionStatus(bad_status)
    elif case in {"skipped_non_skipped_record", "skipped_with_review"}:
        result.run_summary = MultiAgentRunSummary(status=MultiAgentRunStatus.SKIPPED)
        result.review.run_status = MultiAgentRunStatus.SKIPPED
        if case == "skipped_non_skipped_record":
            result.review = None
        else:
            result.tasks[0].status = DiagnosisTaskStatus.SKIPPED
            result.executions[0].status = AgentExecutionStatus.SKIPPED
    elif case == "skipped_with_finding":
        result.run_summary = MultiAgentRunSummary(status=MultiAgentRunStatus.SKIPPED)
        result.review = None
        result.tasks[0].status = DiagnosisTaskStatus.SKIPPED
        result.executions[0].status = AgentExecutionStatus.SKIPPED
    elif case == "empty_review":
        result.review.candidates = []
    elif case == "candidate_without_evidence_chain":
        result.review.candidates[0].supporting_evidence_ids = []
        result.review.candidates[0].contradicting_evidence_ids = []
    return result


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


@pytest.mark.parametrize(
    "status",
    [MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL],
)
def test_orchestrator_runs_v7_after_v5_and_persists_valid_work(status):
    class OrderingRepository(InMemoryInvestigationRepository):
        def save_coordination_review(self, review):
            if review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK:
                investigation_id = review.investigation_id
                assert any(
                    task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
                    for task in self.list_tasks(investigation_id)
                )
                assert any(
                    execution.execution_layer
                    == AgentExecutionLayer.OPENAI_AGENTS_SDK
                    for execution in self.list_executions(investigation_id)
                )
                assert any(
                    finding.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
                    for finding in self.list_agent_findings(investigation_id)
                )
            return super().save_coordination_review(review)

    repository = OrderingRepository()
    runtime = StubAgentsRuntime(repository, status)
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=runtime,
    ).run(load_incident_case("deployment_regression"))

    assert len(runtime.calls) == 1
    assert record.status == InvestigationStatus.COMPLETED
    assert record.hypotheses and record.actions and record.verification_suggestions
    assert record.report is not None
    assert {
        task.execution_layer for task in repository.list_tasks(record.id)
    } == {AgentExecutionLayer.CUSTOM, AgentExecutionLayer.OPENAI_AGENTS_SDK}
    assert {
        execution.execution_layer
        for execution in repository.list_executions(record.id)
    } == {AgentExecutionLayer.CUSTOM, AgentExecutionLayer.OPENAI_AGENTS_SDK}
    assert {
        finding.execution_layer
        for finding in repository.list_agent_findings(record.id)
    } == {AgentExecutionLayer.CUSTOM, AgentExecutionLayer.OPENAI_AGENTS_SDK}
    review = repository.get_coordination_review(record.id)
    assert review.id == f"review-sdk-{status}"
    assert review.run_status == status


def test_orchestrator_accepts_aggregate_review_finding_reference():
    class MixedCandidateRuntime(StubAgentsRuntime):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            baseline = result.review.candidates[0].model_copy(
                update={
                    "id": "candidate-baseline",
                    "rank": 2,
                    "supporting_finding_ids": [],
                    "contradicting_finding_ids": [],
                }
            )
            result.review.candidates.append(baseline)
            return result

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=MixedCandidateRuntime(repository),
    ).run(load_incident_case("deployment_regression"))

    review = repository.get_coordination_review(record.id)
    assert record.status == InvestigationStatus.COMPLETED
    assert review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    assert len(review.candidates) == 2


def test_orchestrator_rejects_forged_partial_agreement_decision():
    class ForgedDecisionRuntime(StubAgentsRuntime):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            result.review.decision_status = CoordinationDecisionStatus.AGREEMENT
            return result

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=ForgedDecisionRuntime(
            repository,
            MultiAgentRunStatus.PARTIAL,
        ),
    ).run(load_incident_case("deployment_regression"))

    sdk_tasks = [
        item
        for item in repository.list_tasks(record.id)
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    assert record.status == InvestigationStatus.COMPLETED
    assert len(sdk_tasks) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )


@pytest.mark.parametrize(
    "selected_cause",
    [CauseType.UNKNOWN, CauseType.TRAFFIC_SPIKE],
)
def test_orchestrator_rejects_forged_agreement_selected_cause(selected_cause):
    class ForgedSelectedCauseRuntime(StubAgentsRuntime):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            baseline = repository.get(kwargs["investigation_id"]).hypotheses[0]
            first = result.findings[0].model_copy(
                update={
                    "finding_type": AgentFindingType.ROOT_CAUSE,
                    "related_cause_type": baseline.cause_type,
                    "confidence": 0.9,
                }
            )
            second_agent = (
                AgentName.METRIC
                if first.agent_name != AgentName.METRIC
                else AgentName.LOG
            )
            second = first.model_copy(
                update={"id": "finding-sdk-agreement-2", "agent_name": second_agent}
            )
            result.findings = [first, second]
            result.review.candidates[0].supporting_finding_ids = [
                first.id,
                second.id,
            ]
            result.review.decision_status = CoordinationDecisionStatus.AGREEMENT
            result.review.baseline_cause_type = baseline.cause_type
            result.review.selected_cause_type = selected_cause
            return result

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=ForgedSelectedCauseRuntime(repository),
    ).run(load_incident_case("deployment_regression"))

    sdk_tasks = [
        item
        for item in repository.list_tasks(record.id)
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    assert record.status == InvestigationStatus.COMPLETED
    assert len(sdk_tasks) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_task_id",
        "empty_tasks",
        "empty_executions",
        "missing_execution",
        "duplicate_per_task",
        "task_layer",
        "duplicate_execution_id",
        "execution_layer",
        "execution_task",
        "execution_agent",
        "execution_round",
        "execution_status",
        "execution_evidence",
        "duplicate_finding_id",
        "finding_collision",
        "finding_layer",
        "finding_investigation",
        "finding_evidence",
        "round2_unknown",
        "round2_cross_layer",
    ],
)
def test_orchestrator_validates_entire_v7_batch_before_any_write(case):
    class InvalidBatchRuntime:
        async def run(self, *, investigation_id, **_kwargs):
            result = sdk_result(
                repository,
                investigation_id,
                MultiAgentRunStatus.COMPLETED,
            )
            return corrupt_sdk_result(result, repository, investigation_id, case)

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=InvalidBatchRuntime(),
    ).run(load_incident_case("deployment_regression"))
    sdk_tasks = [
        task
        for task in repository.list_tasks(record.id)
        if task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_executions = [
        execution
        for execution in repository.list_executions(record.id)
        if execution.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_findings = [
        finding
        for finding in repository.list_agent_findings(record.id)
        if finding.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]

    assert record.status == InvestigationStatus.COMPLETED
    assert len(sdk_tasks) == len(sdk_executions) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert sdk_executions[0].status == AgentExecutionStatus.FAILED
    assert sdk_findings == []
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )
    assert any(
        task.execution_layer == AgentExecutionLayer.CUSTOM
        for task in repository.list_tasks(record.id)
    )
    assert any(
        finding.execution_layer == AgentExecutionLayer.CUSTOM
        for finding in repository.list_agent_findings(record.id)
    )


@pytest.mark.parametrize("record_type", ["task", "execution"])
def test_orchestrator_rejects_v7_id_collision_with_v4(record_type):
    class CollidingRuntime:
        async def run(self, *, investigation_id, **_kwargs):
            result = sdk_result(
                repository,
                investigation_id,
                MultiAgentRunStatus.COMPLETED,
            )
            if record_type == "task":
                self.collided_id = repository.list_tasks(investigation_id)[0].id
                result.tasks[0].id = self.collided_id
                result.executions[0].task_id = self.collided_id
            else:
                self.collided_id = repository.list_executions(investigation_id)[0].id
                result.executions[0].id = self.collided_id
            return result

    repository = InMemoryInvestigationRepository()
    runtime = CollidingRuntime()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=runtime,
    ).run(load_incident_case("deployment_regression"))

    records = (
        repository.list_tasks(record.id)
        if record_type == "task"
        else repository.list_executions(record.id)
    )
    collided = next(item for item in records if item.id == runtime.collided_id)
    assert record.status == InvestigationStatus.COMPLETED
    assert collided.execution_layer == AgentExecutionLayer.CUSTOM
    assert len(
        [
            item
            for item in records
            if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
        ]
    ) == 1


@pytest.mark.parametrize(
    "case",
    [
        "completed_review_none",
        "completed_zero_findings",
        "completed_baseline_only_review",
        "completed_failed_record",
        "partial_review_none",
        "partial_zero_findings",
        "partial_all_completed",
        "partial_all_failed",
        "partial_skipped_record",
        "failed_no_failed_record",
        "failed_with_review",
        "failed_pending_record",
        "failed_running_record",
        "failed_skipped_record",
        "skipped_non_skipped_record",
        "skipped_with_review",
        "skipped_with_finding",
        "empty_review",
        "candidate_without_evidence_chain",
    ],
)
def test_orchestrator_rejects_inconsistent_v7_run_result(case):
    class InvalidRunRuntime:
        async def run(self, *, investigation_id, **_kwargs):
            return invalid_run_result(repository, investigation_id, case)

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=InvalidRunRuntime(),
    ).run(load_incident_case("deployment_regression"))
    sdk_tasks = [
        task
        for task in repository.list_tasks(record.id)
        if task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_findings = [
        finding
        for finding in repository.list_agent_findings(record.id)
        if finding.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]

    assert record.status == InvestigationStatus.COMPLETED
    assert len(sdk_tasks) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert sdk_findings == []
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )


@pytest.mark.parametrize(
    "status",
    [MultiAgentRunStatus.FAILED, MultiAgentRunStatus.SKIPPED],
)
def test_orchestrator_persists_failed_or_skipped_v7_and_keeps_v5_review(status):
    class ReturningRuntime:
        async def run(self, *, investigation_id, **_kwargs):
            result = AgentsRcaRuntimeResult.failed(investigation_id, "safe reason")
            if status == MultiAgentRunStatus.SKIPPED:
                result.tasks[0].status = DiagnosisTaskStatus.SKIPPED
                result.executions[0].status = AgentExecutionStatus.SKIPPED
                result.run_summary = MultiAgentRunSummary(
                    status=status,
                    failure_reason="not configured",
                )
            return result

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=ReturningRuntime(),
    ).run(load_incident_case("deployment_regression"))

    sdk_tasks = [
        task
        for task in repository.list_tasks(record.id)
        if task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_executions = [
        execution
        for execution in repository.list_executions(record.id)
        if execution.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    assert [task.status for task in sdk_tasks] == [DiagnosisTaskStatus(status)]
    assert [execution.status for execution in sdk_executions] == [
        AgentExecutionStatus(status)
    ]
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )
    assert record.status == InvestigationStatus.COMPLETED
    assert record.hypotheses and record.actions and record.verification_suggestions
    assert record.report is not None


def test_orchestrator_rejects_invalid_v7_review_before_any_batch_write():
    class InvalidReviewRuntime(StubAgentsRuntime):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            invalid_candidate = result.review.candidates[0].model_copy(
                update={
                    "supporting_finding_ids": ["finding-unknown"],
                    "supporting_evidence_ids": ["ev-unknown"],
                }
            )
            result.review = result.review.model_copy(
                update={"candidates": [invalid_candidate]}
            )
            return result

    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=InvalidReviewRuntime(repository),
    ).run(load_incident_case("deployment_regression"))

    sdk_tasks = [
        task
        for task in repository.list_tasks(record.id)
        if task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    assert len(sdk_tasks) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert all(
        finding.execution_layer == AgentExecutionLayer.CUSTOM
        for finding in repository.list_agent_findings(record.id)
    )
    assert repository.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )
    assert record.status == InvestigationStatus.COMPLETED


def test_orchestrator_durably_records_redacted_runtime_exception(tmp_path):
    class ExplodingRuntime:
        async def run(self, **_kwargs):
            raise RuntimeError("secret-token-value")

    database_url = f"sqlite:///{tmp_path / 'diagops-v7.db'}"
    engine = create_db_engine(database_url)
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=ExplodingRuntime(),
    ).run(load_incident_case("deployment_regression"))
    engine.dispose()

    reloaded_engine = create_db_engine(database_url)
    initialize_database(reloaded_engine)
    reloaded = SQLiteInvestigationRepository(reloaded_engine)
    stored = reloaded.get(record.id)
    sdk_tasks = [
        task
        for task in reloaded.list_tasks(record.id)
        if task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_executions = [
        execution
        for execution in reloaded.list_executions(record.id)
        if execution.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]

    assert stored.status == InvestigationStatus.COMPLETED
    assert stored.hypotheses and stored.actions and stored.verification_suggestions
    assert stored.report is not None
    assert len(sdk_tasks) == len(sdk_executions) == 1
    assert sdk_tasks[0].status == DiagnosisTaskStatus.FAILED
    assert sdk_tasks[0].analysis_round is None
    assert sdk_executions[0].agent_name == "CoordinatorAgent"
    assert sdk_executions[0].status == AgentExecutionStatus.FAILED
    assert sdk_executions[0].analysis_round is None
    assert "RuntimeError" in sdk_executions[0].error_message
    assert "secret-token-value" not in sdk_executions[0].error_message
    assert reloaded.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.CUSTOM
    )
    reloaded_engine.dispose()


def test_orchestrator_marks_investigation_cancelled_and_reraises():
    class CancelledRuntime:
        async def run(self, **_kwargs):
            raise asyncio.CancelledError

    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(
        repository=repository,
        agents_runtime=CancelledRuntime(),
    )

    with pytest.raises(asyncio.CancelledError):
        orchestrator.run(load_incident_case("deployment_regression"))

    record = repository.list()[0]
    assert record.status == InvestigationStatus.CANCELLED
    assert all(
        item.status != DiagnosisTaskStatus.PENDING
        for item in repository.list_tasks(record.id)
    )
    assert all(
        item.status != AgentExecutionStatus.PENDING
        for item in repository.list_executions(record.id)
    )
    assert all(
        item.execution_layer == AgentExecutionLayer.CUSTOM
        for item in repository.list_tasks(record.id)
    )


@pytest.mark.parametrize(
    "status",
    [
        MultiAgentRunStatus.PARTIAL,
        MultiAgentRunStatus.FAILED,
        MultiAgentRunStatus.SKIPPED,
    ],
)
def test_orchestrator_durably_reloads_v7_result_states(tmp_path, status):
    class ReloadRuntime:
        async def run(self, *, investigation_id, **_kwargs):
            if status == MultiAgentRunStatus.PARTIAL:
                return sdk_result(repository, investigation_id, status)
            result = AgentsRcaRuntimeResult.failed(investigation_id, "safe reason")
            if status == MultiAgentRunStatus.SKIPPED:
                result.tasks[0].status = DiagnosisTaskStatus.SKIPPED
                result.executions[0].status = AgentExecutionStatus.SKIPPED
                result.run_summary = MultiAgentRunSummary(
                    status=status,
                    failure_reason="not configured",
                )
            return result

    database_url = f"sqlite:///{tmp_path / f'diagops-v7-{status}.db'}"
    engine = create_db_engine(database_url)
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=ReloadRuntime(),
    ).run(load_incident_case("deployment_regression"))
    engine.dispose()

    reloaded_engine = create_db_engine(database_url)
    initialize_database(reloaded_engine)
    reloaded = SQLiteInvestigationRepository(reloaded_engine)
    tasks = reloaded.list_tasks(record.id)
    executions = reloaded.list_executions(record.id)
    findings = reloaded.list_agent_findings(record.id)
    review = reloaded.get_coordination_review(record.id)

    assert reloaded.get(record.id).status == InvestigationStatus.COMPLETED
    assert any(item.execution_layer == AgentExecutionLayer.CUSTOM for item in tasks)
    assert any(
        item.execution_layer == AgentExecutionLayer.CUSTOM for item in findings
    )
    assert any(
        item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
        for item in tasks
    )
    assert any(
        item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
        for item in executions
    )
    if status == MultiAgentRunStatus.PARTIAL:
        assert any(
            item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
            for item in findings
        )
        assert review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
        assert review.run_status == status
    else:
        assert all(
            item.execution_layer == AgentExecutionLayer.CUSTOM for item in findings
        )
        assert review.execution_layer == AgentExecutionLayer.CUSTOM
    reloaded_engine.dispose()


def test_orchestrator_retries_v7_persistence_idempotently_once(tmp_path):
    class OneShotExecutionFailureRepository(SQLiteInvestigationRepository):
        failed_once = False

        def save_executions(self, investigation_id, executions):
            if not self.failed_once and any(
                item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
                for item in executions
            ):
                self.failed_once = True
                raise RuntimeError("transient write failure")
            return super().save_executions(investigation_id, executions)

    database_url = f"sqlite:///{tmp_path / 'diagops-v7-retry.db'}"
    engine = create_db_engine(database_url)
    initialize_database(engine)
    repository = OneShotExecutionFailureRepository(engine)
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=StubAgentsRuntime(repository),
    ).run(load_incident_case("deployment_regression"))
    engine.dispose()

    reloaded_engine = create_db_engine(database_url)
    initialize_database(reloaded_engine)
    reloaded = SQLiteInvestigationRepository(reloaded_engine)
    tasks = reloaded.list_tasks(record.id)
    sdk_tasks = [
        item
        for item in tasks
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_executions = [
        item
        for item in reloaded.list_executions(record.id)
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    sdk_findings = [
        item
        for item in reloaded.list_agent_findings(record.id)
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]

    assert reloaded.get(record.id).status == InvestigationStatus.COMPLETED
    assert len({item.id for item in tasks}) == len(tasks)
    assert len(sdk_tasks) == len(sdk_executions) == len(sdk_findings) == 1
    assert reloaded.get_coordination_review(record.id).execution_layer == (
        AgentExecutionLayer.OPENAI_AGENTS_SDK
    )
    reloaded_engine.dispose()


def test_orchestrator_isolates_one_shot_v7_repository_read_failure():
    class OneShotGetFailureRepository(InMemoryInvestigationRepository):
        fail_next_get = False

        def save_coordination_review(self, review):
            saved = super().save_coordination_review(review)
            if review.execution_layer == AgentExecutionLayer.CUSTOM:
                self.fail_next_get = True
            return saved

        def get(self, investigation_id):
            if self.fail_next_get:
                self.fail_next_get = False
                raise RuntimeError("secret-read-failure")
            return super().get(investigation_id)

    class UncalledRuntime:
        async def run(self, **_kwargs):
            raise AssertionError("runtime must not run without persisted input")

    repository = OneShotGetFailureRepository()
    record = build_v2_orchestrator(
        repository=repository,
        agents_runtime=UncalledRuntime(),
    ).run(load_incident_case("deployment_regression"))
    sdk_executions = [
        execution
        for execution in repository.list_executions(record.id)
        if execution.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]

    assert record.status == InvestigationStatus.COMPLETED
    assert record.hypotheses and record.actions and record.verification_suggestions
    assert record.report is not None
    assert len(sdk_executions) == 1
    assert sdk_executions[0].status == AgentExecutionStatus.FAILED
    assert "RuntimeError" in sdk_executions[0].error_message
    assert "secret-read-failure" not in sdk_executions[0].error_message


def test_orchestrator_default_off_adds_no_sdk_records():
    repository = InMemoryInvestigationRepository()
    record = build_v2_orchestrator(repository=repository).run(
        load_incident_case("deployment_regression")
    )

    assert all(
        task.execution_layer == AgentExecutionLayer.CUSTOM
        for task in repository.list_tasks(record.id)
    )
    assert all(
        execution.execution_layer == AgentExecutionLayer.CUSTOM
        for execution in repository.list_executions(record.id)
    )
    assert all(
        finding.execution_layer == AgentExecutionLayer.CUSTOM
        for finding in repository.list_agent_findings(record.id)
    )


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


def test_container_builds_one_agents_runtime_only_when_enabled(monkeypatch):
    import backend.services.container as container_module

    created = []

    class StubRuntime:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(container_module, "AgentsRcaRuntime", StubRuntime, raising=False)

    disabled = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(enabled=False),
        )
    )
    enabled = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(
                enabled=True,
                model="gpt-test",
                max_turns=4,
                timeout_seconds=12,
            ),
        )
    )

    assert disabled.orchestrator.agents_runtime is None
    assert created == [enabled.orchestrator.agents_runtime]
    assert created[0].kwargs == {
        "model": "gpt-test",
        "max_turns": 4,
        "timeout_seconds": 12,
    }


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
