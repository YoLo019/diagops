from time import perf_counter

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceProvider
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider
from backend.providers.mock_related_alerts import MockRelatedAlertProvider
from backend.providers.mock_service_catalog import MockServiceCatalogProvider
from backend.providers.results import ProviderResult, ProviderStatus


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
            results.append(result)
        return results

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for result in self.collect_results(event):
            evidence.extend(result.evidence_items)
            if result.status in {ProviderStatus.FAILED, ProviderStatus.PARTIAL}:
                evidence.append(result.to_error_evidence())
        return sorted(evidence, key=lambda item: item.timestamp)


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
