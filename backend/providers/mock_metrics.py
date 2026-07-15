from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import MetricQuery
from backend.providers.results import ProviderResult


class MockMetricProvider:
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})

    def collect(
        self, event: IncidentEvent, query: MetricQuery | None = None
    ) -> ProviderResult:
        evidence: list[EvidenceItem] = []

        if event.signals.get("qps") == "high":
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=2),
                    summary="QPS increased sharply before latency and errors increased",
                    payload={
                        "qps_change": "+260%",
                        "latency_p95": "1800ms",
                        "root_cause_claims": [
                            {
                                "component": event.service,
                                "reason": "traffic spike",
                                "occurred_at": (
                                    event.started_at - timedelta(minutes=2)
                                ).isoformat(),
                            }
                        ],
                    },
                )
            )
        elif event.service == "payment-service":
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="QPS stayed within normal range while 5xx increased",
                    payload={"qps_change": "+3%", "error_rate": "12%"},
                )
            )

        if event.signals.get("cpu") == "high":
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="One instance has high CPU and abnormal error rate",
                    payload={
                        "instance": "profile-service-3",
                        "cpu": "94%",
                        "error_rate": "18%",
                        "root_cause_claims": [
                            {
                                "component": "profile-service-3",
                                "reason": "resource saturation",
                                "occurred_at": event.started_at.isoformat(),
                            }
                        ],
                    },
                )
            )

        if event.signals.get("database") == "slow":
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=1),
                    summary=(
                        "Database query latency increased before "
                        "report-service latency increased"
                    ),
                    payload={
                        "db_p95": "2400ms",
                        "endpoint": "/reports/daily",
                        "root_cause_claims": [
                            {
                                "component": "database",
                                "reason": "database slowdown",
                                "occurred_at": (
                                    event.started_at - timedelta(minutes=1)
                                ).isoformat(),
                            }
                        ],
                    },
                )
            )

        if query:
            filtered: list[EvidenceItem] = []
            for item in evidence:
                if not query.start_time <= item.timestamp <= query.end_time:
                    continue
                if query.instance and item.payload.get("instance") != query.instance:
                    continue
                if query.metric_names:
                    payload = {
                        name: item.payload[name]
                        for name in query.metric_names
                        if name in item.payload
                    }
                    if "root_cause_claims" in item.payload:
                        payload["root_cause_claims"] = item.payload[
                            "root_cause_claims"
                        ]
                    if not payload:
                        continue
                    item = item.model_copy(update={"payload": payload})
                filtered.append(item)
            evidence = filtered[: query.limit]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
