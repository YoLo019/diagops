from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import LogQuery
from backend.providers.results import ProviderResult


class MockLogProvider:
    provider = EvidenceProvider.LOG
    supported_tools = frozenset({"read_logs"})

    def collect(
        self, event: IncidentEvent, query: LogQuery | None = None
    ) -> ProviderResult:
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
                        "root_cause_claims": [
                            {
                                "component": event.service,
                                "reason": "process or container failure",
                                "occurred_at": (
                                    event.started_at + timedelta(minutes=1)
                                ).isoformat(),
                            }
                        ],
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

        if query:
            evidence = [
                item
                for item in evidence
                if query.start_time <= item.timestamp <= query.end_time
                and (not query.instance or item.payload.get("instance") == query.instance)
                and (
                    not query.levels
                    or "ERROR" in {level.upper() for level in query.levels}
                )
                and (
                    not query.keywords
                    or all(
                        keyword.lower() in f"{item.summary} {item.payload}".lower()
                        for keyword in query.keywords
                    )
                )
            ][: query.limit]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
