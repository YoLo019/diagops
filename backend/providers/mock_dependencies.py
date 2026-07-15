from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import DependencyDirection, DependencyQuery
from backend.providers.results import ProviderResult


class MockDependencyProvider:
    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})

    def collect(
        self, event: IncidentEvent, query: DependencyQuery | None = None
    ) -> ProviderResult:
        evidence: list[EvidenceItem] = []
        if event.service == "order-service":
            evidence = [
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=event.started_at - timedelta(minutes=2),
                    summary=(
                        "inventory-service latency increased before "
                        "order-service timeout errors"
                    ),
                    payload={
                        "dependency": "inventory-service",
                        "latency_p95": "3200ms",
                        "error_rate": "8%",
                    },
                )
            ]

        if event.service == "payment-service":
            evidence = [
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=event.started_at + timedelta(minutes=2),
                    summary="payment dependency latency rose slightly after local errors started",
                    payload={"dependency": "payment-gateway", "latency_p95": "900ms"},
                )
            ]

        if query:
            evidence = [
                item
                for item in evidence
                if query.direction == DependencyDirection.DOWNSTREAM
                and query.start_time <= item.timestamp <= query.end_time
                and (
                    not query.target or item.payload.get("dependency") == query.target
                )
            ][: query.limit]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
