import asyncio
import logging
from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import AgentsRcaRuntime, AgentsRcaRuntimeResult
from backend.diagnosis.coordination_review import (
    build_coordination_review,
    build_hybrid_coordination_review,
)
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.execution_engine import DiagnosisExecutionEngine
from backend.diagnosis.finding_builders import build_agent_findings
from backend.diagnosis.llm_analyst import ReadOnlyLlmAnalyst
from backend.diagnosis.planner import DiagnosisTaskPlanner
from backend.diagnosis.react_agent import ReActInvestigationAgent
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.agent_plan import AgentExecution, DiagnosisTask
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.tools.provider_tools import build_provider_tool_registry

logger = logging.getLogger(__name__)


class DiagnosisOrchestrator:
    def __init__(
        self,
        repository: InMemoryInvestigationRepository,
        providers: ProviderRegistry,
        analyzer: RcaAnalyzer,
        report_generator: ReportGenerator,
        coordinator: DiagnosisCoordinator | None = None,
        action_planner: ActionPlanner | None = None,
        llm_analyst: ReadOnlyLlmAnalyst | None = None,
        task_planner: DiagnosisTaskPlanner | None = None,
        execution_engine: DiagnosisExecutionEngine | None = None,
        react_agent: ReActInvestigationAgent | None = None,
        agents_runtime: AgentsRcaRuntime | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.analyzer = analyzer
        self.report_generator = report_generator
        self.coordinator = coordinator or DiagnosisCoordinator(providers)
        self.action_planner = action_planner or ActionPlanner()
        self.llm_analyst = llm_analyst
        self.task_planner = task_planner or DiagnosisTaskPlanner()
        self.execution_engine = execution_engine or DiagnosisExecutionEngine(
            repository=repository,
            tool_registry=build_provider_tool_registry(providers),
        )
        self.react_agent = react_agent
        self.agents_runtime = agents_runtime

    def run(self, event: IncidentEvent) -> InvestigationRecord:
        record = self.repository.save(
            InvestigationRecord(event=event, status=InvestigationStatus.PENDING)
        )
        logger.info("investigation started id=%s service=%s", record.id, event.service)
        self.repository.update_status(record.id, InvestigationStatus.RUNNING)

        try:
            context = self.coordinator.collect(event)
            evidence = context.evidence
            record.provider_results = context.provider_results
            record.specialist_results = context.specialist_results
            record.evidence = evidence
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            self._record_v4_execution(record.id, event, context.provider_results)

            hypotheses = self.analyzer.analyze(event, evidence)
            record.hypotheses = hypotheses
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            self._record_v5_coordination(record.id, event, evidence, hypotheses)
            v7_result = self._record_v7_coordination(record.id)

            actions, verifications = self.action_planner.plan(event, evidence, hypotheses)
            evidence = self._ensure_action_evidence(evidence, actions)

            record.evidence = evidence
            record.actions = actions
            record.verification_suggestions = verifications
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            self._record_v6_react_trace(record.id, event, evidence)
            if self.llm_analyst is not None:
                record.llm_analysis = self.llm_analyst.analyze(
                    investigation_id=record.id,
                    evidence=evidence,
                    hypotheses=hypotheses,
                )
            report = self.report_generator.generate(
                record.id,
                event,
                evidence,
                hypotheses,
                actions=actions,
                verification_suggestions=verifications,
                **(
                    {
                        "coordination_review": v7_result.review,
                        "multi_agent_run": v7_result.run_summary,
                        "agent_findings": v7_result.findings,
                    }
                    if v7_result is not None
                    else {}
                ),
            )
            record.report = report
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            completed = self.repository.update_status(record.id, InvestigationStatus.COMPLETED)
            logger.info(
                "investigation completed id=%s evidence_count=%s "
                "action_count=%s verification_count=%s",
                completed.id,
                len(completed.evidence),
                len(completed.actions),
                len(completed.verification_suggestions),
            )
            return completed
        except asyncio.CancelledError:
            try:
                self.repository.update_status(
                    record.id,
                    InvestigationStatus.CANCELLED,
                )
            except Exception as exc:
                logger.warning(
                    "investigation cancellation persistence failed id=%s "
                    "error_type=%s",
                    record.id,
                    type(exc).__name__,
                )
            raise
        except Exception as exc:
            logger.exception("investigation failed id=%s reason=%s", record.id, exc)
            record.failure_reason = str(exc)
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            return self.repository.update_status(
                record.id,
                InvestigationStatus.FAILED,
                failure_reason=str(exc),
            )

    def _record_v5_coordination(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses,
    ) -> None:
        try:
            findings = build_agent_findings(investigation_id, event, evidence)
            cause_rank = {
                hypothesis.cause_type: index for index, hypothesis in enumerate(hypotheses)
            }
            findings.sort(
                key=lambda finding: cause_rank.get(
                    finding.related_cause_type, len(cause_rank)
                )
            )
            save_findings = getattr(self.repository, "save_agent_findings", None)
            if save_findings is not None:
                save_findings(investigation_id, findings)

            review = build_coordination_review(
                investigation_id, findings, evidence, hypotheses
            )
            save_review = getattr(self.repository, "save_coordination_review", None)
            if save_review is not None:
                save_review(review)
        except Exception as exc:
            logger.warning(
                "v5 coordination recording failed id=%s reason=%s",
                investigation_id,
                exc,
                exc_info=True,
            )

    def _record_v7_coordination(
        self,
        investigation_id: str,
    ) -> AgentsRcaRuntimeResult | None:
        if self.agents_runtime is None:
            return None

        evidence: list[EvidenceItem] = []
        hypotheses: list[Hypothesis] = []
        existing_task_ids: set[str] = set()
        existing_execution_ids: set[str] = set()
        existing_finding_ids: set[str] = set()
        try:
            persisted = self.repository.get(investigation_id)
            evidence = persisted.evidence
            hypotheses = persisted.hypotheses
            existing_task_ids = {
                item.id for item in self.repository.list_tasks(investigation_id)
            }
            existing_execution_ids = {
                item.id for item in self.repository.list_executions(investigation_id)
            }
            existing_finding_ids = {
                item.id
                for item in self.repository.list_agent_findings(investigation_id)
            }
            result = asyncio.run(
                self.agents_runtime.run(
                    investigation_id=investigation_id,
                    event=persisted.event,
                    evidence=persisted.evidence,
                    hypotheses=persisted.hypotheses,
                )
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: Agents runtime failed"
            logger.warning(
                "v7 agents runtime failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            result = AgentsRcaRuntimeResult.failed(investigation_id, reason)

        try:
            result = self._validate_v7_result(
                investigation_id,
                result,
                evidence,
                hypotheses,
                existing_task_ids,
                existing_execution_ids,
                existing_finding_ids,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: Invalid agents runtime result"
            logger.warning(
                "v7 agents validation failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            result = self._validate_v7_result(
                investigation_id,
                AgentsRcaRuntimeResult.failed(investigation_id, reason),
                evidence,
                hypotheses,
                existing_task_ids,
                existing_execution_ids,
                existing_finding_ids,
            )

        for attempt in range(2):
            try:
                self._persist_v7_result(investigation_id, result)
            except Exception as exc:
                if attempt == 1:
                    logger.warning(
                        "v7 agents persistence failed id=%s error_type=%s",
                        investigation_id,
                        type(exc).__name__,
                    )
            reloaded = self._reload_v7_result(investigation_id, result)
            if reloaded is not None:
                return reloaded
        return None

    def _validate_v7_result(
        self,
        investigation_id: str,
        result: AgentsRcaRuntimeResult,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        existing_task_ids: set[str],
        existing_execution_ids: set[str],
        existing_finding_ids: set[str],
    ) -> AgentsRcaRuntimeResult:
        evidence_ids = {item.id for item in evidence}
        tasks = [
            DiagnosisTask.model_validate(item.model_dump(mode="python"))
            for item in result.tasks
        ]
        executions = [
            AgentExecution.model_validate(item.model_dump(mode="python"))
            for item in result.executions
        ]
        findings = [
            AgentFinding.model_validate(item.model_dump(mode="python"))
            for item in result.findings
        ]
        summary = MultiAgentRunSummary.model_validate(
            result.run_summary.model_dump(mode="python")
        )
        task_by_id = {item.id: item for item in tasks}
        if (
            not tasks
            or len(task_by_id) != len(tasks)
            or not task_by_id.keys().isdisjoint(existing_task_ids)
            or any(
                item.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
                for item in tasks
            )
        ):
            raise ValueError("Invalid V7 tasks")

        execution_ids = {item.id for item in executions}
        execution_task_ids = [item.task_id for item in executions]
        if (
            not executions
            or len(execution_ids) != len(executions)
            or not execution_ids.isdisjoint(existing_execution_ids)
            or len(execution_task_ids) != len(set(execution_task_ids))
            or set(execution_task_ids) != set(task_by_id)
            or any(
                item.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
                or item.agent_name != task_by_id[item.task_id].agent_name
                or item.analysis_round != task_by_id[item.task_id].analysis_round
                or item.status.value != task_by_id[item.task_id].status.value
                or not set(item.evidence_ids).issubset(evidence_ids)
                for item in executions
            )
        ):
            raise ValueError("Invalid V7 executions")

        finding_by_id = {item.id: item for item in findings}
        if (
            len(finding_by_id) != len(findings)
            or not finding_by_id.keys().isdisjoint(existing_finding_ids)
            or any(
                item.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
                or item.investigation_id != investigation_id
                or not set(item.evidence_ids).issubset(evidence_ids)
                for item in findings
            )
        ):
            raise ValueError("Invalid V7 findings")
        for finding in findings:
            if finding.analysis_round != 2:
                continue
            revised = finding_by_id.get(finding.revises_finding_id)
            if (
                revised is None
                or revised.analysis_round != 1
                or revised.agent_name != finding.agent_name
                or revised.investigation_id != investigation_id
            ):
                raise ValueError("Invalid V7 finding revision")

        review = None
        if result.review is not None:
            review = CoordinationReview.model_validate(
                result.review.model_dump(mode="python")
            )
            expected = build_hybrid_coordination_review(
                investigation_id,
                findings,
                evidence,
                hypotheses,
                summary.status,
                review.summary,
                review.uncertainty,
            )
            if (
                summary.status
                not in {MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL}
                or review.investigation_id != investigation_id
                or review.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
                or review.run_status != summary.status
                or (
                    review.decision_status,
                    review.baseline_cause_type,
                    review.selected_cause_type,
                )
                != (
                    expected.decision_status,
                    expected.baseline_cause_type,
                    expected.selected_cause_type,
                )
                or not review.candidates
                or not any(
                    candidate.supporting_finding_ids
                    or candidate.contradicting_finding_ids
                    for candidate in review.candidates
                )
                or any(
                    not (
                        candidate.supporting_evidence_ids
                        or candidate.contradicting_evidence_ids
                    )
                    or not set(
                        candidate.supporting_evidence_ids
                        + candidate.contradicting_evidence_ids
                    ).issubset(evidence_ids)
                    or not set(
                        candidate.supporting_finding_ids
                        + candidate.contradicting_finding_ids
                    ).issubset(finding_by_id)
                    for candidate in review.candidates
                )
            ):
                raise ValueError("Invalid V7 coordination review")

        statuses = {item.status.value for item in tasks}
        valid_run = {
            MultiAgentRunStatus.COMPLETED: review is not None
            and statuses == {"completed"}
            and bool(findings),
            MultiAgentRunStatus.PARTIAL: review is not None
            and statuses == {"completed", "failed"}
            and bool(findings),
            MultiAgentRunStatus.FAILED: review is None
            and statuses <= {"completed", "failed"}
            and "failed" in statuses,
            MultiAgentRunStatus.SKIPPED: review is None
            and statuses == {"skipped"}
            and not findings,
        }[summary.status]
        if not valid_run:
            raise ValueError("Invalid V7 run status")

        return AgentsRcaRuntimeResult(
            tasks=tasks,
            executions=executions,
            findings=findings,
            review=review,
            run_summary=summary,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_names=list(result.tool_names),
        )

    def _persist_v7_result(
        self,
        investigation_id: str,
        result: AgentsRcaRuntimeResult,
    ) -> None:
        tasks = {item.id: item for item in self.repository.list_tasks(investigation_id)}
        tasks.update({item.id: item for item in result.tasks})
        self.repository.save_tasks(
            investigation_id,
            list(tasks.values()),
        )
        self.repository.save_executions(investigation_id, result.executions)
        self.repository.save_agent_findings(investigation_id, result.findings)
        if result.review is not None:
            self.repository.save_coordination_review(result.review)

    def _reload_v7_result(
        self,
        investigation_id: str,
        result: AgentsRcaRuntimeResult,
    ) -> AgentsRcaRuntimeResult | None:
        try:
            task_by_id = {
                item.id: item for item in self.repository.list_tasks(investigation_id)
            }
            execution_by_id = {
                item.id: item
                for item in self.repository.list_executions(investigation_id)
            }
            finding_by_id = {
                item.id: item
                for item in self.repository.list_agent_findings(investigation_id)
            }
            tasks = [
                DiagnosisTask.model_validate(
                    task_by_id[item.id].model_dump(mode="python")
                )
                for item in result.tasks
            ]
            executions = [
                AgentExecution.model_validate(
                    execution_by_id[item.id].model_dump(mode="python")
                )
                for item in result.executions
            ]
            findings = [
                AgentFinding.model_validate(
                    finding_by_id[item.id].model_dump(mode="python")
                )
                for item in result.findings
            ]
            if any(
                persisted.model_dump(mode="json") != expected.model_dump(mode="json")
                for expected_items, persisted_items in (
                    (result.tasks, tasks),
                    (result.executions, executions),
                    (result.findings, findings),
                )
                for expected, persisted in zip(
                    expected_items, persisted_items, strict=True
                )
            ):
                return None

            if result.review is None:
                coordinator = next(
                    (
                        item
                        for item in reversed(executions)
                        if item.agent_name == "CoordinatorAgent"
                        and item.status.value in {"failed", "skipped"}
                    ),
                    None,
                )
                if coordinator is None:
                    return None
                review = None
                summary = MultiAgentRunSummary(
                    status=MultiAgentRunStatus(coordinator.status.value),
                    failure_reason=coordinator.error_message,
                )
            else:
                persisted_review = self.repository.get_coordination_review(
                    investigation_id
                )
                if persisted_review is None:
                    return None
                review = CoordinationReview.model_validate(
                    persisted_review.model_dump(mode="python")
                )
                if review.model_dump(mode="json") != result.review.model_dump(
                    mode="json"
                ):
                    return None
                summary = MultiAgentRunSummary(status=review.run_status)

            return AgentsRcaRuntimeResult(
                tasks=tasks,
                executions=executions,
                findings=findings,
                review=review,
                run_summary=summary,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_names=list(result.tool_names),
            )
        except Exception as exc:
            logger.warning(
                "v7 agents persistence reload failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            return None

    def _record_v4_execution(
        self,
        investigation_id: str,
        event: IncidentEvent,
        provider_results: list[ProviderResult],
    ) -> None:
        try:
            plan = self.task_planner.plan(event, investigation_id=investigation_id)
            self.execution_engine.run(plan, event, provider_results=provider_results)
        except Exception as exc:
            logger.warning(
                "v4 execution recording failed id=%s reason=%s",
                investigation_id,
                exc,
                exc_info=True,
            )

    def _record_v6_react_trace(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
    ) -> None:
        if self.react_agent is None:
            return

        try:
            trace = self.react_agent.run(
                investigation_id=investigation_id,
                event=event,
                existing_evidence_ids=[item.id for item in evidence],
            )
            save_trace = getattr(self.repository, "save_react_trace", None)
            if callable(save_trace):
                save_trace(trace)
        except Exception as exc:
            logger.warning(
                "v6 react trace failed id=%s reason=%s",
                investigation_id,
                exc,
                exc_info=True,
            )

    def _ensure_action_evidence(
        self,
        evidence: list[EvidenceItem],
        actions,
    ) -> list[EvidenceItem]:
        evidence_ids = {item.id for item in evidence}
        needs_missing = any(
            evidence_id == "ev-missing-evidence" and evidence_id not in evidence_ids
            for action in actions
            for evidence_id in action.supporting_evidence_ids
        )
        if not needs_missing:
            return evidence

        return [
            *evidence,
            EvidenceItem(
                id="ev-missing-evidence",
                provider=EvidenceProvider.SERVICE_CATALOG,
                kind=EvidenceKind.PROVIDER_ERROR,
                status=EvidenceStatus.FAILED,
                timestamp=datetime.now(UTC),
                summary="Additional evidence is required before recommending action.",
                payload={"reason": "action planner had no concrete evidence"},
                confidence=1.0,
                error_message="missing evidence",
            ),
        ]
