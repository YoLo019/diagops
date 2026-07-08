import logging
from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordination_review import build_coordination_review
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.execution_engine import DiagnosisExecutionEngine
from backend.diagnosis.finding_builders import build_agent_findings
from backend.diagnosis.llm_analyst import ReadOnlyLlmAnalyst
from backend.diagnosis.planner import DiagnosisTaskPlanner
from backend.diagnosis.react_agent import ReActInvestigationAgent
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
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
