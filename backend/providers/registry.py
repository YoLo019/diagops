from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider


class ProviderRegistry:
    def __init__(self, providers: list[EvidenceProviderProtocol]) -> None:
        self.providers = providers

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for provider in self.providers:
            evidence.extend(provider.collect(event))
        return sorted(evidence, key=lambda item: item.timestamp)


def build_mock_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        providers=[
            MockLogProvider(),
            MockMetricProvider(),
            MockDeployProvider(),
            MockDependencyProvider(),
        ]
    )
