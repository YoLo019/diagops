from pathlib import Path

from backend.config.settings import AppSettings, StorageSettings, load_settings
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.llm_analyst import ReadOnlyLlmAnalyst
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.providers.registry import build_provider_registry_from_settings
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator


class AppContainer:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self.repository = self._build_repository()
        providers = build_provider_registry_from_settings(self.settings)
        llm_analyst = (
            ReadOnlyLlmAnalyst(enabled=True) if self.settings.llm.enabled else None
        )
        self.orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=providers,
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            action_planner=ActionPlanner(),
            llm_analyst=llm_analyst,
        )

    def _build_repository(self):
        storage_url = self.settings.storage.url
        if storage_url == "memory://":
            return InMemoryInvestigationRepository()
        if storage_url.startswith("sqlite"):
            self._ensure_sqlite_parent_directory(storage_url)
            engine = create_db_engine(storage_url)
            initialize_database(engine)
            return SQLiteInvestigationRepository(engine)
        raise ValueError(f"Unsupported storage URL: {storage_url}")

    def _ensure_sqlite_parent_directory(self, storage_url: str) -> None:
        if storage_url == "sqlite:///:memory:":
            return
        path_prefix = "sqlite:///"
        if not storage_url.startswith(path_prefix):
            return
        database_path = Path(storage_url.removeprefix(path_prefix))
        parent = database_path.parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)


_container: AppContainer | None = None


def get_container() -> AppContainer:
    global _container
    if _container is None:
        _container = AppContainer()
    return _container


def reset_container(settings: AppSettings | None = None) -> AppContainer:
    global _container
    test_settings = settings or AppSettings(storage=StorageSettings(url="memory://"))
    _container = AppContainer(settings=test_settings)
    return _container
