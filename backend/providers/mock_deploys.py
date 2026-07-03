from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockDeployProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        if event.service != "payment-service":
            return []

        return [
            EvidenceItem(
                provider=EvidenceProvider.DEPLOY,
                kind=EvidenceKind.DEPLOYMENT,
                timestamp=event.started_at - timedelta(minutes=3),
                summary="payment-service v1.8.2 was deployed three minutes before 5xx increased",
                payload={
                    "version": "v1.8.2",
                    "commit": "abc1234",
                    "changed_module": "PayConfirmHandler",
                },
            )
        ]
