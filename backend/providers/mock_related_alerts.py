from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockRelatedAlertProvider:
    provider = EvidenceProvider.RELATED_ALERT

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.RELATED_ALERT,
                timestamp=event.started_at,
                summary=f"No wider alert storm detected around {event.service}",
                payload={
                    "service": event.service,
                    "related_alert_count": 0,
                    "time_window_minutes": event.time_window_minutes,
                },
                confidence=0.8,
            )
        ]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
