from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator


class AppContainer:
    def __init__(self) -> None:
        self.repository = InMemoryInvestigationRepository()
        self.orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=build_mock_provider_registry(),
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
        )


container = AppContainer()
