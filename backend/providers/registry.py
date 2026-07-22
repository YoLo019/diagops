import asyncio
import logging
from collections.abc import Callable
from time import perf_counter

from backend.config.settings import AppSettings
from backend.domain.events import IncidentEvent, IncidentSource
from backend.domain.evidence import EvidenceItem, EvidenceProvider
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.file_deployments import FileDeploymentProvider
from backend.providers.file_logs import FileLogProvider
from backend.providers.file_service_catalog import FileServiceCatalogProvider
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider
from backend.providers.mock_related_alerts import MockRelatedAlertProvider
from backend.providers.mock_service_catalog import MockServiceCatalogProvider
from backend.providers.prometheus import PrometheusProvider
from backend.providers.results import ProviderResult, ProviderStatus
from backend.runtime.concurrency import RunStepGate
from backend.safety.redaction import redact_model, safe_failure

logger = logging.getLogger(__name__)

_PROVIDER_BY_TOOL = {
    "read_logs": EvidenceProvider.LOG,
    "query_metrics": EvidenceProvider.METRIC,
    "query_prometheus": EvidenceProvider.METRIC,
    "read_deployments": EvidenceProvider.DEPLOY,
    "read_service_catalog": EvidenceProvider.SERVICE_CATALOG,
    "query_dependencies": EvidenceProvider.DEPENDENCY,
}


class ProviderRegistry:
    def __init__(
        self,
        providers: list[EvidenceProviderProtocol],
        simulation_providers: list[EvidenceProviderProtocol] | None = None,
    ) -> None:
        self.providers = providers
        self.simulation_providers = simulation_providers or []

    def _providers_for(self, event: IncidentEvent) -> list[EvidenceProviderProtocol]:
        if event.source == IncidentSource.SIMULATED:
            return [*self.providers, *self.simulation_providers]
        return list(self.providers)

    def collect_results(self, event: IncidentEvent) -> list[ProviderResult]:
        results = [
            self._collect_provider(provider, event)
            for provider in self._providers_for(event)
        ]
        return self._with_unconfigured(results)

    async def collect_results_async(
        self,
        event: IncidentEvent,
        *,
        max_parallel_steps: int = 3,
        check_execution: Callable[[], None] | None = None,
        parallel_limit: RunStepGate | None = None,
    ) -> list[ProviderResult]:
        """按配置上限并行只读 Provider；gather 顺序保持既有输出稳定。"""
        if max_parallel_steps < 1:
            raise ValueError("max_parallel_steps must be positive")
        limit = parallel_limit or RunStepGate(max_parallel_steps)
        guard = check_execution or (lambda: None)

        async def collect(provider) -> ProviderResult:
            async with limit.slot():
                guard()
                result = await asyncio.to_thread(self._collect_provider, provider, event)
                guard()
                return result

        results = list(
            await asyncio.gather(
                *(collect(provider) for provider in self._providers_for(event))
            )
        )
        return self._with_unconfigured(results)

    def _collect_provider(self, provider, event: IncidentEvent) -> ProviderResult:
        started = perf_counter()
        try:
            result = redact_model(provider.collect(event))
        except Exception:
            provider_name = getattr(provider, "provider", EvidenceProvider.LOG)
            result = ProviderResult(
                provider=provider_name,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
                duration_ms=int((perf_counter() - started) * 1000),
            )
        logger.info(
            "provider completed provider=%s status=%s duration_ms=%s evidence_count=%s",
            result.provider,
            result.status,
            result.duration_ms,
            len(result.evidence_items),
        )
        return result

    @staticmethod
    def _with_unconfigured(results: list[ProviderResult]) -> list[ProviderResult]:
        results = list(results)
        configured = {result.provider for result in results}
        results.extend(
            ProviderResult(
                provider=provider,
                status=ProviderStatus.SKIPPED,
                error_message=f"{provider.value} provider not configured",
            )
            for provider in EvidenceProvider
            if provider not in configured
        )
        return results

    def query_results(self, event, tool_name, query) -> list[ProviderResult]:
        providers = [
            provider
            for provider in self._providers_for(event)
            if tool_name in getattr(provider, "supported_tools", ())
        ]
        if not providers:
            return [
                ProviderResult(
                    provider=_PROVIDER_BY_TOOL[tool_name],
                    status=ProviderStatus.SKIPPED,
                    error_message=f"{tool_name} provider not configured",
                )
            ]

        results: list[ProviderResult] = []
        for provider in providers:
            started = perf_counter()
            try:
                result = redact_model(provider.collect(event, query))
            except Exception:
                result = ProviderResult(
                    provider=getattr(provider, "provider", _PROVIDER_BY_TOOL[tool_name]),
                    status=ProviderStatus.FAILED,
                    error_message=safe_failure("provider_failure"),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            results.append(result)
        return results

    def evidence_from_results(self, results: list[ProviderResult]) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for result in results:
            evidence.extend(result.evidence_items)
            if result.status in {
                ProviderStatus.FAILED,
                ProviderStatus.PARTIAL,
                ProviderStatus.SKIPPED,
            }:
                evidence.append(result.to_error_evidence())
        return sorted(evidence, key=lambda item: item.timestamp)

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        return self.evidence_from_results(self.collect_results(event))


def build_mock_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        providers=[],
        simulation_providers=[
            MockLogProvider(),
            MockMetricProvider(),
            MockDeployProvider(),
            MockDependencyProvider(),
            MockServiceCatalogProvider(),
            MockRelatedAlertProvider(),
        ]
    )


def build_provider_registry_from_settings(settings: AppSettings) -> ProviderRegistry:
    providers: list[EvidenceProviderProtocol] = []
    simulation_providers: list[EvidenceProviderProtocol] = []

    if settings.providers.mock.enabled:
        simulation_providers.extend(
            [
                MockLogProvider(),
                MockMetricProvider(),
                MockDeployProvider(),
                MockDependencyProvider(),
                MockServiceCatalogProvider(),
                MockRelatedAlertProvider(),
            ]
        )

    if settings.providers.service_catalog.enabled:
        providers.append(FileServiceCatalogProvider(settings.providers.service_catalog.path))

    if settings.providers.deployment_file.enabled:
        providers.append(FileDeploymentProvider(settings.providers.deployment_file.path))

    if settings.providers.log_file.enabled:
        providers.append(FileLogProvider(settings.providers.log_file.paths))

    if settings.providers.prometheus.enabled:
        providers.append(PrometheusProvider(settings.providers.prometheus.base_url))

    return ProviderRegistry(
        providers=providers,
        simulation_providers=simulation_providers,
    )
