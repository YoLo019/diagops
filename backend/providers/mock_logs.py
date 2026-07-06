from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockLogProvider:
    provider = EvidenceProvider.LOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence: list[EvidenceItem] = []
        if event.service == "payment-service":
            evidence = [
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.LOG_PATTERN,
                    timestamp=event.started_at + timedelta(minutes=1),
                    summary="New NullPointerException appears in /pay/confirm after deployment",
                    payload={
                        "exception": "NullPointerException",
                        "endpoint": "/pay/confirm",
                        "count": 120,
                    },
                )
            ]

        if event.service == "order-service":
            evidence = [
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.LOG_PATTERN,
                    timestamp=event.started_at,
                    summary="TimeoutException increased when calling inventory-service",
                    payload={
                        "exception": "TimeoutException",
                        "dependency": "inventory-service",
                        "count": 90,
                    },
                )
            ]

        return ProviderResult(provider=self.provider, evidence_items=evidence)
