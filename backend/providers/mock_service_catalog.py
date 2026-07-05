from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockServiceCatalogProvider:
    provider = EvidenceProvider.SERVICE_CATALOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.SERVICE_METADATA,
                timestamp=event.started_at,
                summary=f"{event.service} owner is platform-team",
                payload={
                    "service": event.service,
                    "owner": "platform-team",
                    "runtime": "python",
                    "environment": event.environment,
                    "dependencies": ["inventory-service", "payment-db"],
                },
                confidence=1.0,
            )
        ]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
