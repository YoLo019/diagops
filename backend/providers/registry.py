import logging
from time import perf_counter

from backend.config.settings import AppSettings
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceProvider
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.file_service_catalog import FileServiceCatalogProvider
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider
from backend.providers.mock_related_alerts import MockRelatedAlertProvider
from backend.providers.mock_service_catalog import MockServiceCatalogProvider
from backend.providers.results import ProviderResult, ProviderStatus

logger = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self, providers: list[EvidenceProviderProtocol]) -> None:
        self.providers = providers

    def collect_results(self, event: IncidentEvent) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        for provider in self.providers:
            started = perf_counter()
            try:
                result = provider.collect(event)
            except Exception as exc:
                provider_name = getattr(provider, "provider", EvidenceProvider.LOG)
                result = ProviderResult(
                    provider=provider_name,
                    status=ProviderStatus.FAILED,
                    error_message=str(exc),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            logger.info(
                "provider completed provider=%s status=%s duration_ms=%s evidence_count=%s",
                result.provider,
                result.status,
                result.duration_ms,
                len(result.evidence_items),
            )
            results.append(result)
        return results

    def evidence_from_results(self, results: list[ProviderResult]) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for result in results:
            evidence.extend(result.evidence_items)
            if result.status in {ProviderStatus.FAILED, ProviderStatus.PARTIAL}:
                evidence.append(result.to_error_evidence())
        return sorted(evidence, key=lambda item: item.timestamp)

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        return self.evidence_from_results(self.collect_results(event))


def build_mock_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        providers=[
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

    if settings.providers.mock.enabled:
        providers.extend(
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

    return ProviderRegistry(providers=providers)
