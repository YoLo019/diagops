from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockDependencyProvider:
    provider = EvidenceProvider.DEPENDENCY

    def collect(self, event: IncidentEvent) -> ProviderResult:
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

        return ProviderResult(provider=self.provider, evidence_items=evidence)
