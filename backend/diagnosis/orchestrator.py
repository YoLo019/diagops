from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator


class DiagnosisOrchestrator:
    def __init__(
        self,
        repository: InMemoryInvestigationRepository,
        providers: ProviderRegistry,
        analyzer: RcaAnalyzer,
        report_generator: ReportGenerator,
        coordinator: DiagnosisCoordinator | None = None,
        action_planner: ActionPlanner | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.analyzer = analyzer
        self.report_generator = report_generator
        self.coordinator = coordinator or DiagnosisCoordinator(providers)
        self.action_planner = action_planner or ActionPlanner()

    def run(self, event: IncidentEvent) -> InvestigationRecord:
        record = self.repository.save(
            InvestigationRecord(event=event, status=InvestigationStatus.PENDING)
        )
        self.repository.update_status(record.id, InvestigationStatus.RUNNING)

        try:
            context = self.coordinator.collect(event)
            evidence = context.evidence
            hypotheses = self.analyzer.analyze(event, evidence)
            actions, verifications = self.action_planner.plan(event, evidence, hypotheses)
            evidence = self._ensure_action_evidence(evidence, actions)

            record.evidence = evidence
            record.hypotheses = hypotheses
            record.actions = actions
            record.verification_suggestions = verifications
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
            return self.repository.update_status(record.id, InvestigationStatus.COMPLETED)
        except Exception as exc:
            record.failure_reason = str(exc)
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            return self.repository.update_status(
                record.id,
                InvestigationStatus.FAILED,
                failure_reason=str(exc),
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
