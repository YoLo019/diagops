from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import DeploymentQuery
from backend.providers.results import ProviderResult


class MockDeployProvider:
    provider = EvidenceProvider.DEPLOY
    supported_tools = frozenset({"read_deployments"})

    def collect(
        self, event: IncidentEvent, query: DeploymentQuery | None = None
    ) -> ProviderResult:
        evidence: list[EvidenceItem] = []
        if event.service != "payment-service":
            return ProviderResult(provider=self.provider, evidence_items=evidence)

        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.DEPLOYMENT,
                timestamp=event.started_at - timedelta(minutes=3),
                summary="payment-service v1.8.2 was deployed three minutes before 5xx increased",
                payload={
                    "version": "v1.8.2",
                    "commit": "abc1234",
                    "changed_module": "PayConfirmHandler",
                    "root_cause_claims": [
                        {
                            "component": event.service,
                            "reason": "deployment regression",
                            "occurred_at": (
                                event.started_at - timedelta(minutes=3)
                            ).isoformat(),
                        }
                    ],
                },
            )
        ]
        if query:
            evidence = [
                item
                for item in evidence
                if query.start_time <= item.timestamp <= query.end_time
                and (not query.version or item.payload.get("version") == query.version)
                and (not query.instance or item.payload.get("instance") == query.instance)
            ][: query.limit]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
