from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockMetricProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []

        if event.signals.get("qps") == "high":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=2),
                    summary="QPS increased sharply before latency and errors increased",
                    payload={"qps_change": "+260%", "latency_p95": "1800ms"},
                )
            )
        elif event.service == "payment-service":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="QPS stayed within normal range while 5xx increased",
                    payload={"qps_change": "+3%", "error_rate": "12%"},
                )
            )

        if event.signals.get("cpu") == "high":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="One instance has high CPU and abnormal error rate",
                    payload={
                        "instance": "profile-service-3",
                        "cpu": "94%",
                        "error_rate": "18%",
                    },
                )
            )

        if event.signals.get("database") == "slow":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=1),
                    summary=(
                        "Database query latency increased before "
                        "report-service latency increased"
                    ),
                    payload={"db_p95": "2400ms", "endpoint": "/reports/daily"},
                )
            )

        return evidence
