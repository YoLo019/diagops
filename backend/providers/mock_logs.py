from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockLogProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        if event.service == "payment-service":
            return [
                EvidenceItem(
                    provider=EvidenceProvider.LOG,
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
            return [
                EvidenceItem(
                    provider=EvidenceProvider.LOG,
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

        return []
