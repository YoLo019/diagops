import asyncio
import logging
import os
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from backend.benchmarks.openrca.evaluator import evaluate_persisted_prediction
from backend.config.settings import AppSettings, StorageSettings, load_settings
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.deepseek_model import create_deepseek_model
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.events import IncidentEvent
from backend.domain.multi_agent import (
    AuthorityMode,
    ExecutionContractVersion,
    ModelProvider,
)
from backend.domain.runtime import (
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
)
from backend.providers.registry import build_provider_registry_from_settings
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.diff import RuntimeDiffService
from backend.runtime.event_hub import EventHub
from backend.runtime.manager import RuntimeManager
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import InMemoryRuntimeStore, RuntimeConflict
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeWriter
from backend.safety.redaction import redact_model
from backend.tools.provider_tools import build_provider_tool_registry

logger = logging.getLogger(__name__)
_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class AppContainer:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown = False
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
                    prompt_version="v9",
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
        self.event_hub = EventHub()
        self.telemetry = RuntimeTelemetry.from_settings(
            self.settings.runtime.opentelemetry
        )
        self.runtime_store = self._build_runtime_store()
        self.runtime_writer = RuntimeWriter(self.runtime_store)
        self.runtime_phase_executor = DiagnosisPhaseExecutor(
            self.orchestrator,
            max_parallel_steps_per_run=(
                self.settings.runtime.max_parallel_steps_per_run
            ),
            telemetry=self.telemetry,
        )
        self.runtime_manager = RuntimeManager(
            coordinator_factory=self._build_runtime_coordinator,
            max_concurrent_runs=self.settings.runtime.max_concurrent_runs,
        )
        self.replay_service = ReplayService(
            ReplayDependencies(
                store=self.runtime_store,
                benchmark_evaluator=partial(
                    evaluate_persisted_prediction,
                    self.runtime_store,
                ),
            )
        )
        self.diff_service = RuntimeDiffService(store=self.runtime_store)

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

    def _build_runtime_store(self):
        if isinstance(self.repository, InMemoryInvestigationRepository):
            return InMemoryRuntimeStore(
                self.repository,
                lease_seconds=self.settings.runtime.lease_seconds,
                event_publisher=self.event_hub.publish,
            )
        if isinstance(self.repository, SQLiteInvestigationRepository):
            if self.engine is None:
                raise RuntimeError("SQLite Runtime requires the shared database engine")
            return SQLiteRuntimeStore(
                self.engine,
                self.repository,
                lease_seconds=self.settings.runtime.lease_seconds,
                event_publisher=self.event_hub.publish,
            )
        raise ValueError("Unsupported repository for Runtime")

    def _build_runtime_coordinator(self, _run_id: str) -> RuntimeCoordinator:
        return RuntimeCoordinator(
            store=self.runtime_store,
            writer=self.runtime_writer,
            phase_executor=self.runtime_phase_executor,
            heartbeat_seconds=self.settings.runtime.heartbeat_seconds,
            telemetry=self.telemetry,
        )

    async def startup(self) -> None:
        """按持久化先行顺序启动 Runtime，过期 lease 只审计不自动恢复。"""
        if not self.settings.runtime.enabled:
            return
        await self.runtime_writer.start()
        self.runtime_store.audit_expired_leases(datetime.now(UTC))
        await self.runtime_manager.startup()

    async def shutdown(self) -> None:
        """在有界时间内停止新调度并清空已接受的 Runtime 写入。"""
        async with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
            try:
                await asyncio.wait_for(self.runtime_manager.shutdown(), timeout=15)
            except TimeoutError:
                logger.warning("runtime shutdown timed out category=runtime_shutdown_timeout")
            finally:
                try:
                    await self.runtime_writer.shutdown()
                finally:
                    self.telemetry.force_flush(
                        timeout_seconds=_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS
                    )
                    self.telemetry.shutdown(
                        timeout_seconds=_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS
                    )
                    self.close()

    async def run_investigation(
        self,
        event: IncidentEvent,
        *,
        strategy=None,
        execution_contract_version: ExecutionContractVersion = (
            ExecutionContractVersion.V10_LEGACY
        ),
        runtime_run_id: str | None = None,
    ):
        if ExecutionContractVersion(execution_contract_version) == ExecutionContractVersion.V11:
            if not self.settings.runtime.enabled:
                raise RuntimeConflict("V11 execution requires enabled Runtime")
            if runtime_run_id is None:
                raise RuntimeConflict(
                    "V11 service execution requires a persisted RuntimeRun"
                )
            run = self.runtime_store.get_run(runtime_run_id)
            if (
                not run.is_v11
                or getattr(event, "investigation_id", run.investigation_id)
                not in {None, run.investigation_id}
            ):
                raise RuntimeConflict("V11 RuntimeRun does not own this service request")
            await self.runtime_writer.start()
            task = await self.runtime_manager.start(run.id)
            await task
            return self.repository.get(run.investigation_id)
        if not self.settings.runtime.enabled:
            return await asyncio.to_thread(
                self.orchestrator.run,
                event,
                strategy=strategy,
                execution_contract_version=execution_contract_version,
            )
        effective_strategy = strategy or self.settings.agents.strategy
        record = redact_model(
            InvestigationRecord(event=event, strategy=effective_strategy)
        )
        self.repository.save(record)
        run = self.create_runtime_run(
            record.id,
            strategy=effective_strategy,
            run_reason=RuntimeRunReason.INITIAL,
        )
        await self.runtime_writer.start()
        task = await self.runtime_manager.start(run.id)
        try:
            await task
        except Exception:
            failed = self.repository.get(record.id)
            if failed.status != InvestigationStatus.FAILED:
                raise
            return failed
        return self.repository.get(record.id)

    def create_runtime_run(
        self,
        investigation_id: str,
        *,
        strategy,
        run_reason: RuntimeRunReason,
        parent_run_id: str | None = None,
        model_provider=None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        execution_contract_version: ExecutionContractVersion = (
            ExecutionContractVersion.V10_LEGACY
        ),
    ) -> RuntimeRun:
        version = ExecutionContractVersion(execution_contract_version)
        source_record = self.repository.get(investigation_id)
        if version == ExecutionContractVersion.V11:
            investigation_id = self._v11_investigation_id(
                source_record,
                strategy=strategy,
            )
        agents_runtime = self.orchestrator.agents_runtime
        effective_provider = model_provider or getattr(
            agents_runtime, "model_provider", None
        )
        effective_model = model_name or getattr(agents_runtime, "_model_name", None)
        effective_prompt = prompt_version or getattr(
            agents_runtime, "prompt_version", None
        )
        authority = (
            AuthorityMode.AGENT
            if version == ExecutionContractVersion.V11
            else AuthorityMode.LEGACY_DETERMINISTIC
        )
        contract = {}
        if version == ExecutionContractVersion.V11:
            contract = {
                "execution_contract_version": version.value,
                "authority_mode": authority.value,
                "model_provider": effective_provider.value
                if isinstance(effective_provider, ModelProvider)
                else effective_provider,
                "model_name": effective_model,
                "prompt_version": effective_prompt,
                "tool_budget": self.settings.agents.max_total_tool_calls,
                "token_budget": getattr(agents_runtime, "runtime_token_budget", None),
                "timeout_seconds": float(self.settings.agents.timeout_seconds),
            }
        run = RuntimeRun(
            investigation_id=investigation_id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=strategy,
            run_reason=run_reason,
            parent_run_id=parent_run_id,
            model_provider=effective_provider,
            model_name=effective_model,
            prompt_version=effective_prompt,
            tool_budget=self.settings.agents.max_total_tool_calls,
            token_budget=getattr(agents_runtime, "runtime_token_budget", None),
            timeout_seconds=float(self.settings.agents.timeout_seconds),
            execution_contract_version=version,
            authority_mode=authority,
            execution_contract=contract,
        )
        return self.runtime_store.create_run(run)

    def _v11_investigation_id(self, source: InvestigationRecord, *, strategy) -> str:
        """为 legacy 历史投影创建隔离的 V11 investigation，保留源记录不变。"""
        if source.active_runtime_run_id is not None:
            return source.id
        has_projection = any(
            (
                source.status != InvestigationStatus.PENDING,
                bool(source.evidence),
                bool(source.provider_results),
                bool(source.specialist_results),
                bool(source.hypotheses),
                source.report is not None,
                source.llm_analysis is not None,
                source.multi_agent_run is not None,
                bool(source.actions),
                bool(source.verification_suggestions),
                self.repository.get_plan(source.id) is not None,
                bool(self.repository.list_tasks(source.id)),
                bool(self.repository.list_context_facts(source.id)),
                bool(self.repository.list_tool_calls(source.id)),
                bool(self.repository.list_agent_findings(source.id)),
                bool(self.repository.list_executions(source.id)),
                self.repository.get_coordination_review(source.id) is not None,
                self.repository.get_react_trace(source.id) is not None,
            )
        )
        if not has_projection:
            return source.id
        linked = InvestigationRecord(
            event=source.event,
            strategy=strategy,
            runtime_available=source.runtime_available,
            source_investigation_id=source.id,
        )
        return self.repository.save(linked).id

    def replay_run(self, run_id: str):
        return self.replay_service.replay(run_id)

    def diff_runs(self, run_id: str, against_run_id: str):
        return self.diff_service.compare(run_id, against_run_id)

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
