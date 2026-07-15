import logging
import os
from pathlib import Path

from backend.config.settings import AppSettings, StorageSettings, load_settings
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.deepseek_model import create_deepseek_model
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.multi_agent import ModelProvider
from backend.providers.registry import build_provider_registry_from_settings
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.tools.provider_tools import build_provider_tool_registry

logger = logging.getLogger(__name__)


class AppContainer:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self.engine = None
        self.repository = self._build_repository()
        providers = build_provider_registry_from_settings(self.settings)
        agents_runtime = None
        if self.settings.agents.enabled:
            try:
                provider = self.settings.agents.provider
                model = self.settings.agents.model
                if provider == ModelProvider.DEEPSEEK:
                    model = create_deepseek_model(
                        model,
                        os.getenv("DEEPSEEK_API_KEY"),
                    )
                agents_runtime = AgentsRcaRuntime(
                    model=model,
                    max_turns=self.settings.agents.max_turns,
                    timeout_seconds=self.settings.agents.timeout_seconds,
                    model_provider=provider,
                    model_name=self.settings.agents.model,
                    strategy=self.settings.agents.strategy,
                    tool_registry=build_provider_tool_registry(providers),
                    max_tool_calls_per_specialist=(
                        self.settings.agents.max_tool_calls_per_specialist
                    ),
                    max_total_tool_calls=(
                        self.settings.agents.max_total_tool_calls
                    ),
                    tool_timeout_seconds=(
                        self.settings.agents.tool_timeout_seconds
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "agents runtime construction failed error_type=%s",
                    type(exc).__name__,
                )
        self.orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=providers,
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            action_planner=ActionPlanner(),
            agents_runtime=agents_runtime,
            default_strategy=self.settings.agents.strategy,
            max_tool_calls_per_specialist=(
                self.settings.agents.max_tool_calls_per_specialist
            ),
            max_total_tool_calls=self.settings.agents.max_total_tool_calls,
        )

    def _build_repository(self):
        storage_url = self.settings.storage.url
        if storage_url == "memory://":
            return InMemoryInvestigationRepository()
        if storage_url.startswith("sqlite"):
            self._ensure_sqlite_parent_directory(storage_url)
            self.engine = create_db_engine(storage_url)
            initialize_database(self.engine)
            return SQLiteInvestigationRepository(self.engine)
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

    def close(self) -> None:
        """释放仅由当前 container 创建并持有的数据库 engine。"""
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


_container: AppContainer | None = None


def get_container() -> AppContainer:
    global _container
    if _container is None:
        _container = AppContainer()
    return _container


def reset_container(settings: AppSettings | None = None) -> AppContainer:
    global _container
    test_settings = settings or AppSettings(storage=StorageSettings(url="memory://"))
    replacement = AppContainer(settings=test_settings)
    previous = _container
    _container = replacement
    if previous is not None:
        previous.close()
    return _container
