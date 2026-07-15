from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import ServiceCatalogQuery
from backend.providers.results import ProviderResult


class MockServiceCatalogProvider:
    provider = EvidenceProvider.SERVICE_CATALOG
    supported_tools = frozenset({"read_service_catalog"})

    def collect(
        self, event: IncidentEvent, query: ServiceCatalogQuery | None = None
    ) -> ProviderResult:
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
        if query and not query.include_dependencies:
            evidence[0] = evidence[0].model_copy(
                update={"payload": {**evidence[0].payload, "dependencies": []}}
            )
        return ProviderResult(provider=self.provider, evidence_items=evidence)
