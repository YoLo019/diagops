from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.events import IncidentEvent
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
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.analyzer = analyzer
        self.report_generator = report_generator

    def run(self, event: IncidentEvent) -> InvestigationRecord:
        evidence = self.providers.collect_all(event)
        hypotheses = self.analyzer.analyze(event, evidence)

        record = InvestigationRecord(
            event=event,
            status=InvestigationStatus.COMPLETED,
            evidence=evidence,
            hypotheses=hypotheses,
        )
        report = self.report_generator.generate(record.id, event, evidence, hypotheses)
        record.report = report

        return self.repository.save(record)
