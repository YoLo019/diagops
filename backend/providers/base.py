from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.providers.results import ProviderResult


class EvidenceProviderProtocol(Protocol):
    provider: object

    def collect(self, event: IncidentEvent) -> ProviderResult:
        """Collect evidence for an incident event."""
