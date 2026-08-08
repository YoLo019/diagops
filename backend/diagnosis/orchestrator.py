import asyncio
import logging
from datetime import UTC, datetime

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import (
    AgentsRcaRuntime,
    AgentsRcaRuntimeResult,
    stabilization_categories_from_executions,
)
from backend.diagnosis.coordination_review import (
    build_coordination_review,
    build_hybrid_coordination_review,
)
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_agent_semantics,
    validate_investigation_evidence,
)
from backend.diagnosis.execution_engine import DiagnosisExecutionEngine
from backend.diagnosis.finding_builders import build_agent_findings
from backend.diagnosis.planner import DiagnosisTaskPlanner
from backend.diagnosis.result_validation import AgentResultValidationError
from backend.domain.agent_findings import (
    AgentFinding,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisTask,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import (
    AdaptiveRunStatus,
    AdaptiveStopReason,
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ExecutionContractVersion,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
    ResultValidationCategory,
)
from backend.domain.tool_calls import ToolCallRecord
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.tools.provider_tools import build_provider_tool_registry

logger = logging.getLogger(__name__)


def _candidate_contract(candidate: RootCauseCandidate) -> dict[str, object]:
    return candidate.model_dump(mode="python", exclude={"id"})


def _unique_models(items: list[ProviderResult]) -> list[ProviderResult]:
    unique: list[ProviderResult] = []
    fingerprints: set[str] = set()
    for item in items:
        validated = ProviderResult.model_validate(item.model_dump(mode="python"))
        fingerprint = validated.model_dump_json()
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unique.append(validated)
    return unique


def _merge_evidence(
    existing: list[EvidenceItem], additional: list[EvidenceItem]
) -> list[EvidenceItem]:
    merged = [
        EvidenceItem.model_validate(item.model_dump(mode="python"))
        for item in existing
    ]
    by_id = {item.id: item for item in merged}
    for item in additional:
        validated = EvidenceItem.model_validate(item.model_dump(mode="python"))
        previous = by_id.get(validated.id)
        if previous is not None:
            if previous != validated:
                raise EvidenceContractError(
                    "duplicate_evidence_id", validated.id
                )
            continue
        by_id[validated.id] = validated
        merged.append(validated)
    return merged


class DiagnosisOrchestrator:
    def __init__(
        self,
        repository: InMemoryInvestigationRepository,
        providers: ProviderRegistry,
        analyzer: RcaAnalyzer,
        report_generator: ReportGenerator,
        coordinator: DiagnosisCoordinator | None = None,
        action_planner: ActionPlanner | None = None,
        task_planner: DiagnosisTaskPlanner | None = None,
        execution_engine: DiagnosisExecutionEngine | None = None,
        agents_runtime: AgentsRcaRuntime | None = None,
        v11_runtime=None,
        default_strategy: InvestigationStrategy | None = None,
        max_tool_calls_per_specialist: int = 3,
        max_total_tool_calls: int = 8,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.analyzer = analyzer
        self.report_generator = report_generator
        self.coordinator = coordinator or DiagnosisCoordinator(providers)
        self.action_planner = action_planner or ActionPlanner()
        self.task_planner = task_planner or DiagnosisTaskPlanner()
        self.execution_engine = execution_engine or DiagnosisExecutionEngine(
            repository=repository,
            tool_registry=build_provider_tool_registry(providers),
        )
        self.agents_runtime = agents_runtime
        self.v11_runtime = v11_runtime
        self.default_strategy = InvestigationStrategy(
            default_strategy
            or getattr(agents_runtime, "strategy", InvestigationStrategy.FIXED)
        )
        self.max_tool_calls_per_specialist = max_tool_calls_per_specialist
        self.max_total_tool_calls = max_total_tool_calls

    def run(
        self,
        event: IncidentEvent,
        strategy: InvestigationStrategy | None = None,
        execution_contract_version: ExecutionContractVersion = (
            ExecutionContractVersion.V10_LEGACY
        ),
    ) -> InvestigationRecord:
        # 延迟导入避免 Runtime 适配层与既有编排器产生模块循环。
        from backend.runtime.phase_executor import DiagnosisPhaseExecutor

        return asyncio.run(
            DiagnosisPhaseExecutor(self).execute(
                event,
                strategy,
                execution_contract_version=execution_contract_version,
            )
        ).record

    def _record_v5_coordination(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses,
    ) -> None:
        self._record_v5_findings(
            investigation_id,
            event,
            evidence,
            hypotheses,
        )
        self._record_v5_review(investigation_id, evidence, hypotheses)

    def _record_v5_findings(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses,
    ) -> list[AgentFinding]:
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
            self.repository.save_agent_findings(investigation_id, findings)
            return findings
        except Exception as exc:
            logger.warning(
                "v5 finding recording failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            return []

    def _record_v5_review(
        self,
        investigation_id: str,
        evidence: list[EvidenceItem],
        hypotheses,
    ) -> CoordinationReview | None:
        try:
            findings = self.repository.list_agent_findings(investigation_id)
            review = build_coordination_review(
                investigation_id, findings, evidence, hypotheses
            )
            validate_agent_semantics(
                evidence, findings, review.candidates, review.root_causes
            )
            self.repository.save_coordination_review(review)
            return review
        except Exception as exc:
            logger.warning(
                "v5 review recording failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            return None

    def _record_v7_coordination(
        self,
        investigation_id: str,
        strategy: InvestigationStrategy,
    ) -> AgentsRcaRuntimeResult | None:
        """保留旧同步调用面；Runtime Phase 路径直接 await async 实现。"""
        return asyncio.run(
            self._record_v7_coordination_async(investigation_id, strategy)
        )

    async def _record_v7_coordination_async(
        self,
        investigation_id: str,
        strategy: InvestigationStrategy,
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
            supporting_evidence = validate_investigation_evidence(
                persisted.provider_results
            ).supporting_evidence
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
            result = await self.agents_runtime.run(
                investigation_id=investigation_id,
                event=persisted.event,
                evidence=supporting_evidence,
                hypotheses=persisted.hypotheses,
                strategy=strategy,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: Agents runtime failed"
            logger.warning(
                "v7 agents runtime failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )
            result = AgentsRcaRuntimeResult.failed(investigation_id, reason)
            if strategy == InvestigationStrategy.ADAPTIVE:
                result.run_summary = result.run_summary.model_copy(
                    update={
                        "strategy": strategy,
                        "adaptive_status": AdaptiveRunStatus.DEGRADED,
                        "adaptive_stop_reason": AdaptiveStopReason.FAILED,
                        "max_tool_calls_per_specialist": (
                            self.max_tool_calls_per_specialist
                        ),
                        "max_total_tool_calls": self.max_total_tool_calls,
                    }
                )

        try:
            evidence = self._persist_adaptive_artifacts(
                investigation_id, result
            )
        except Exception:
            return self._record_v7_persistence_failure(
                investigation_id, result
            )

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
        except AgentResultValidationError as exc:
            logger.warning(
                "v7 agents validation failed id=%s validation_category=%s",
                investigation_id,
                exc.category.value,
            )
            fallback = AgentsRcaRuntimeResult.validation_failed(
                exc.category,
                model_provider=result.run_summary.model_provider,
                model_name=result.run_summary.model_name,
            )
            self._copy_adaptive_metadata(fallback, result, degraded=True)
            result = self._validate_v7_result(
                investigation_id,
                fallback,
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
            fallback = AgentsRcaRuntimeResult.failed(
                investigation_id, reason
            )
            self._copy_adaptive_metadata(fallback, result, degraded=True)
            result = self._validate_v7_result(
                investigation_id,
                fallback,
                evidence,
                hypotheses,
                existing_task_ids,
                existing_execution_ids,
                existing_finding_ids,
            )

        try:
            self._persist_v7_result(investigation_id, result)
        except Exception:
            return self._record_v7_persistence_failure(
                investigation_id, result
            )
        return self._reload_v7_result(investigation_id, result)

    def _resolved_run_summary(
        self,
        strategy: InvestigationStrategy,
        result: AgentsRcaRuntimeResult | None,
    ) -> MultiAgentRunSummary | None:
        if result is not None:
            return result.run_summary
        if strategy != InvestigationStrategy.ADAPTIVE:
            return None
        return MultiAgentRunSummary(
            status=MultiAgentRunStatus.SKIPPED,
            strategy=strategy,
            adaptive_status=AdaptiveRunStatus.SKIPPED,
            max_tool_calls_per_specialist=self.max_tool_calls_per_specialist,
            max_total_tool_calls=self.max_total_tool_calls,
        )

    def _persist_adaptive_artifacts(
        self,
        investigation_id: str,
        result: AgentsRcaRuntimeResult,
    ) -> list[EvidenceItem]:
        record = self.repository.get(investigation_id)
        new_provider_results = _unique_models(result.provider_results)
        for provider_result in new_provider_results:
            validate_investigation_evidence([provider_result])
        provider_results = _unique_models(
            [*record.provider_results, *new_provider_results]
        )
        evidence = _merge_evidence(record.evidence, result.evidence)
        calls = [
            ToolCallRecord.model_validate(item.model_dump(mode="python"))
            for item in result.tool_calls
        ]
        record.provider_results = provider_results
        record.evidence = evidence
        record.updated_at = datetime.now(UTC)
        self.repository.save(record)
        if calls:
            self.repository.save_tool_calls(investigation_id, calls)
        return evidence

    @staticmethod
    def _copy_adaptive_metadata(
        target: AgentsRcaRuntimeResult,
        source: AgentsRcaRuntimeResult,
        *,
        degraded: bool,
    ) -> None:
        target.run_summary.strategy = source.run_summary.strategy
        target.run_summary.adaptive_status = (
            AdaptiveRunStatus.DEGRADED
            if degraded
            and source.run_summary.strategy == InvestigationStrategy.ADAPTIVE
            else source.run_summary.adaptive_status
        )
        target.run_summary.adaptive_stop_reason = (
            source.run_summary.adaptive_stop_reason
        )
        target.run_summary.tool_call_count = source.run_summary.tool_call_count
        target.run_summary.max_tool_calls_per_specialist = (
            source.run_summary.max_tool_calls_per_specialist
        )
        target.run_summary.max_total_tool_calls = (
            source.run_summary.max_total_tool_calls
        )
        target.tool_calls = list(source.tool_calls)
        target.provider_results = list(source.provider_results)
        target.evidence = list(source.evidence)

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
        try:
            tasks = [
                DiagnosisTask.model_validate(item.model_dump(mode="python"))
                for item in result.tasks
            ]
        except (AttributeError, TypeError, ValueError) as exc:
            raise AgentResultValidationError(
                ResultValidationCategory.TASK_CONTRACT
            ) from exc
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
            raise AgentResultValidationError(
                ResultValidationCategory.TASK_CONTRACT
            )

        try:
            executions = [
                AgentExecution.model_validate(item.model_dump(mode="python"))
                for item in result.executions
            ]
        except (AttributeError, TypeError, ValueError) as exc:
            raise AgentResultValidationError(
                ResultValidationCategory.EXECUTION_CONTRACT
            ) from exc
        execution_ids = {item.id for item in executions}
        execution_task_ids = [item.task_id for item in executions]
        execution_by_task_id = {item.task_id: item for item in executions}
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
            raise AgentResultValidationError(
                ResultValidationCategory.EXECUTION_CONTRACT
            )

        try:
            findings = [
                AgentFinding.model_validate(item.model_dump(mode="python"))
                for item in result.findings
            ]
        except (AttributeError, TypeError, ValueError) as exc:
            raise AgentResultValidationError(
                ResultValidationCategory.FINDING_CONTRACT
            ) from exc
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
            raise AgentResultValidationError(
                ResultValidationCategory.FINDING_CONTRACT
            )
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
                raise AgentResultValidationError(
                    ResultValidationCategory.FINDING_REVISION_CONTRACT
                )

        try:
            summary = MultiAgentRunSummary.model_validate(
                result.run_summary.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AgentResultValidationError(
                ResultValidationCategory.RUN_STATUS_CONTRACT
            ) from exc

        review = None
        if result.review is not None:
            try:
                review = CoordinationReview.model_validate(
                    result.review.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise AgentResultValidationError(
                    ResultValidationCategory.REVIEW_CONTRACT
                ) from exc
            if review.model_provider is None:
                raise AgentResultValidationError(
                    ResultValidationCategory.REVIEW_ATTRIBUTION
                )
            persisted_review = self.repository.get_coordination_review(
                investigation_id
            )
            authoritative_root_causes = (
                list(persisted_review.root_causes)
                if persisted_review is not None
                else []
            )
            review.root_causes = authoritative_root_causes
            expected = build_hybrid_coordination_review(
                investigation_id,
                findings,
                evidence,
                hypotheses,
                summary.status,
                review.summary,
                review.uncertainty,
                model_provider=review.model_provider,
                model_name=review.model_name,
                primary_stabilization_category=(
                    review.primary_stabilization_category
                ),
                secondary_stabilization_categories=(
                    review.secondary_stabilization_categories
                ),
                root_causes=authoritative_root_causes,
            )
            candidate_contracts = [
                _candidate_contract(candidate) for candidate in review.candidates
            ]
            expected_candidate_contracts = [
                _candidate_contract(candidate) for candidate in expected.candidates
            ]
            has_finding_support = any(
                candidate.supporting_finding_ids
                or candidate.contradicting_finding_ids
                for candidate in review.candidates
            )
            allows_deterministic_fallback = (
                review.decision_status == CoordinationDecisionStatus.FALLBACK
                and candidate_contracts == expected_candidate_contracts
            )
            if (
                summary.status
                not in {MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL}
                or review.investigation_id != investigation_id
                or review.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
                or review.run_status != summary.status
                or review.model_provider != summary.model_provider
                or review.model_name != summary.model_name
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
                or not (has_finding_support or allows_deterministic_fallback)
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
                raise AgentResultValidationError(
                    ResultValidationCategory.REVIEW_CONTRACT
                )
            try:
                validate_agent_semantics(
                    [
                        item
                        for item in evidence
                        if item.status
                        in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
                        and item.kind != EvidenceKind.PROVIDER_ERROR
                    ],
                    findings,
                    review.candidates,
                    review.root_causes,
                )
            except EvidenceContractError as exc:
                raise AgentResultValidationError(
                    ResultValidationCategory.SEMANTIC_REFERENCE
                ) from exc

        recovered_agents = {
            execution.agent_name
            for execution in executions
            if execution.attempt == 2
            and execution.step_kind == ExecutionStepKind.SPECIALIST_RECOLLECTION
            and execution.status.value == "completed"
        }
        resolved_failure_task_ids = {
            task.id
            for task in tasks
            if task.status.value == "failed"
            and (
                execution := execution_by_task_id[task.id]
            ).status.value
            == "failed"
            and execution.step_kind == ExecutionStepKind.SPECIALIST_COLLECTION
            and execution.attempt == 1
            and execution.failure_category == FailureCategory.MISSING_SPECIALIST
            and execution.agent_name in recovered_agents
        }
        statuses = {
            task.status.value
            for task in tasks
            if task.id not in resolved_failure_task_ids
        }
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
            raise AgentResultValidationError(
                ResultValidationCategory.RUN_STATUS_CONTRACT
            )

        return AgentsRcaRuntimeResult(
            tasks=tasks,
            executions=executions,
            findings=findings,
            review=review,
            run_summary=summary,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_names=list(result.tool_names),
            tool_calls=list(result.tool_calls),
            provider_results=list(result.provider_results),
            evidence=list(result.evidence),
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
        self.repository.save_multi_agent_result(
            investigation_id,
            result.findings,
            result.executions,
            result.review,
        )

    def _record_v7_persistence_failure(
        self,
        investigation_id: str,
        result: AgentsRcaRuntimeResult,
    ) -> AgentsRcaRuntimeResult | None:
        execution = AgentExecution(
            task_id=f"task-review-persistence-{investigation_id}",
            agent_name="CoordinatorAgent",
            status=AgentExecutionStatus.FAILED,
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            step_kind=ExecutionStepKind.REVIEW_PERSISTENCE,
            attempt=1,
            failure_category=FailureCategory.PERSISTENCE,
            model_provider=result.run_summary.model_provider,
            model_name=result.run_summary.model_name,
            error_message="Agents review persistence failed",
        )
        recovery = AgentsRcaRuntimeResult(
            tasks=[],
            executions=[execution],
            findings=[],
            review=None,
            run_summary=MultiAgentRunSummary(
                status=MultiAgentRunStatus.FAILED,
                failure_reason="Agents review persistence failed",
                model_provider=result.run_summary.model_provider,
                model_name=result.run_summary.model_name,
                primary_stabilization_category=(
                    stabilization_categories_from_executions([execution])[0]
                ),
            ),
        )
        self._copy_adaptive_metadata(recovery, result, degraded=True)
        try:
            self.repository.save_multi_agent_result(
                investigation_id, [], [execution], None
            )
        except Exception:
            logger.warning(
                "v7 agents persistence recovery failed id=%s "
                "failure_category=%s",
                investigation_id,
                FailureCategory.PERSISTENCE.value,
            )
            return None
        return self._reload_v7_result(investigation_id, recovery)

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
                categories = stabilization_categories_from_executions(executions)
                summary = result.run_summary.model_copy(
                    update={
                        "status": MultiAgentRunStatus(coordinator.status.value),
                        "failure_reason": coordinator.error_message,
                        "model_provider": coordinator.model_provider,
                        "model_name": coordinator.model_name,
                        "primary_stabilization_category": (
                            categories[0] if categories else None
                        ),
                        "secondary_stabilization_categories": categories[1:],
                    }
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
                summary = result.run_summary.model_copy(
                    update={
                        "status": review.run_status,
                        "failure_reason": None,
                        "model_provider": review.model_provider,
                        "model_name": review.model_name,
                        "primary_stabilization_category": (
                            review.primary_stabilization_category
                        ),
                        "secondary_stabilization_categories": (
                            review.secondary_stabilization_categories
                        ),
                    }
                )

            return AgentsRcaRuntimeResult(
                tasks=tasks,
                executions=executions,
                findings=findings,
                review=review,
                run_summary=summary,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_names=list(result.tool_names),
                tool_calls=list(result.tool_calls),
                provider_results=list(result.provider_results),
                evidence=list(result.evidence),
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
                "v4 execution recording failed id=%s error_type=%s",
                investigation_id,
                type(exc).__name__,
            )

    def _ensure_action_evidence(
        self,
        evidence: list[EvidenceItem],
        actions,
        investigation_id: str | None = None,
    ) -> list[EvidenceItem]:
        evidence_ids = {item.id for item in evidence}
        needs_missing = any(
            evidence_id == "ev-missing-evidence" and evidence_id not in evidence_ids
            for action in actions
            for evidence_id in action.supporting_evidence_ids
        )
        if not needs_missing:
            return evidence

        missing_evidence_id = (
            f"ev-missing-evidence-{investigation_id}"
            if investigation_id is not None
            else "ev-missing-evidence"
        )
        for action in actions:
            action.supporting_evidence_ids = [
                missing_evidence_id if item == "ev-missing-evidence" else item
                for item in action.supporting_evidence_ids
            ]

        return [
            *evidence,
            EvidenceItem(
                id=missing_evidence_id,
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
