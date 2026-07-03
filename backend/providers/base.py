from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem


class EvidenceProviderProtocol(Protocol):
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        """Collect evidence for an incident event."""
