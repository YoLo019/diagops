import asyncio
import json
import logging
from collections.abc import Callable
from time import perf_counter

from openai import APIConnectionError, RateLimitError

from backend.config.settings import AppSettings
from backend.domain.events import IncidentEvent, IncidentSource
from backend.domain.evidence import EvidenceItem, EvidenceProvider
from backend.domain.multi_agent import FailureCategory
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.file_deployments import FileDeploymentProvider
from backend.providers.file_logs import FileLogProvider
from backend.providers.file_service_catalog import FileServiceCatalogProvider
from backend.providers.local_package import (
    LocalIncidentPackage,
    build_package_providers,
)
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
    "query_traces": EvidenceProvider.TRACE,
    "read_runtime_state": EvidenceProvider.RUNTIME_STATE,
    "query_related_alerts": EvidenceProvider.RELATED_ALERT,
}

_LEGACY_EVIDENCE_PROVIDERS = (
    EvidenceProvider.LOG,
    EvidenceProvider.METRIC,
    EvidenceProvider.DEPLOY,
    EvidenceProvider.DEPENDENCY,
    EvidenceProvider.SERVICE_CATALOG,
    EvidenceProvider.RELATED_ALERT,
)


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
            self._collect_provider(provider, event) for provider in self._providers_for(event)
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
            await asyncio.gather(*(collect(provider) for provider in self._providers_for(event)))
        )
        return self._with_unconfigured(results)

    def _collect_provider(self, provider, event: IncidentEvent) -> ProviderResult:
        started = perf_counter()
        try:
            result = _bounded_result(redact_model(provider.collect(event)), 100)
        except Exception as exc:
            provider_name = getattr(provider, "provider", EvidenceProvider.LOG)
            result = ProviderResult(
                provider=provider_name,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
                duration_ms=int((perf_counter() - started) * 1000),
                failure_category=_provider_failure_category(exc),
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
            for provider in _LEGACY_EVIDENCE_PROVIDERS
            if provider not in configured
        )
        return results

    def supports_tool(self, tool_name: str, event: IncidentEvent) -> bool:
        """仅声明当前事件可用的只读 Provider 能力。"""
        return any(
            tool_name in getattr(provider, "supported_tools", ())
            for provider in self._providers_for(event)
        )

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
        remaining = query.limit
        for provider in providers:
            started = perf_counter()
            try:
                result = redact_model(provider.collect(event, query))
                result = _bounded_result(result, remaining)
                remaining -= len(result.evidence_items)
            except Exception as exc:
                result = ProviderResult(
                    provider=getattr(provider, "provider", _PROVIDER_BY_TOOL[tool_name]),
                    status=ProviderStatus.FAILED,
                    error_message=safe_failure("provider_failure"),
                    duration_ms=int((perf_counter() - started) * 1000),
                    failure_category=_provider_failure_category(exc),
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


def _bounded_result(result: ProviderResult, limit: int) -> ProviderResult:
    """限制入库结果；保留失败状态和截断提示，细节通过收窄查询获取。"""
    bounded = [
        item
        for item in result.evidence_items[:limit]
        if len(json.dumps(item.payload, ensure_ascii=False).encode("utf-8")) <= 16384
    ]
    truncated = (
        result.truncated
        or len(bounded) != len(result.evidence_items)
        or any(len(item.summary) > 512 for item in bounded)
    )
    # 未截断且 Provider 未声明计数时保持旧序列化；工具响应单独给返回量。
    if not truncated and result.returned_count is None:
        return result
    usable = result.status in {ProviderStatus.SUCCESS, ProviderStatus.PARTIAL}
    return result.model_copy(
        update={
            "evidence_items": [
                item.model_copy(update={"summary": item.summary[:512]}) for item in bounded
            ],
            "truncated": truncated,
            "returned_count": len(bounded),
            "status": ProviderStatus.PARTIAL if truncated and usable else result.status,
            "error_message": "query_result_truncated: narrow query filters"
            if truncated and usable
            else result.error_message,
        }
    )


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
        ],
    )


def _provider_failure_category(exc: BaseException) -> FailureCategory | None:
    if isinstance(exc, RateLimitError):
        return FailureCategory.RATE_LIMIT
    if isinstance(exc, APIConnectionError):
        return FailureCategory.TRANSPORT
    return None


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

    if settings.providers.local_package.enabled:
        providers.extend(build_local_package_providers(settings.providers.local_package.path))

    return ProviderRegistry(
        providers=providers,
        simulation_providers=simulation_providers,
    )


def build_local_package_providers(path) -> list[EvidenceProviderProtocol]:
    """从离线 incident package 构造全部本地 Provider；package 损坏即显式失败。"""
    return build_package_providers(LocalIncidentPackage.load(path))
