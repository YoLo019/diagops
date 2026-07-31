from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.domain.tool_queries import QueryWindow
from backend.providers.results import ProviderResult


class EvidenceProviderProtocol(Protocol):
    provider: object

    def collect(self, event: IncidentEvent, query: QueryWindow | None = None) -> ProviderResult:
        """Collect evidence for an incident event."""
