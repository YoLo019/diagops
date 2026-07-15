import json
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
    JsonValue,
)
from backend.domain.tool_queries import MetricAggregation, PrometheusQuery
from backend.providers.results import ProviderResult, ProviderStatus

_QUERY_TEMPLATES = {
    "qps": 'sum(rate(http_requests_total{{service="{service}",environment="{environment}"}}[5m]))',
    "5xx_rate": (
        'sum(rate(http_requests_total{{service="{service}",environment="{environment}",'
        'status=~"5.."}}[5m]))'
    ),
    "p95_latency": (
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        '{{service="{service}",environment="{environment}"}}[5m])) by (le))'
    ),
    "cpu": (
        'sum(rate(container_cpu_usage_seconds_total'
        '{{container="{service}",environment="{environment}"}}[5m]))'
    ),
    "memory": (
        'sum(container_memory_usage_bytes'
        '{{container="{service}",environment="{environment}"}})'
    ),
}


class PrometheusProvider:
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_prometheus"})

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def collect(
        self, event: IncidentEvent, query: PrometheusQuery | None = None
    ) -> ProviderResult:
        queries = _queries_for_event(event, query)
        query_time = query.end_time.timestamp() if query else event.started_at.timestamp()
        observed_values: dict[str, JsonValue] = {}
        failures: list[str] = []
        for name, promql in queries.items():
            try:
                observed_values[name] = self._query_instant(promql, query_time)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if not observed_values:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message="; ".join(failures),
            )
        status = ProviderStatus.PARTIAL if failures else ProviderStatus.SUCCESS

        evidence = EvidenceItem(
            provider=self.provider,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=(query.end_time if query else event.started_at) - timedelta(minutes=1),
            summary=(
                f"Prometheus returned {len(observed_values)} metric observations "
                f"for {event.service} in {event.environment}"
            ),
            payload={
                "service": event.service,
                "environment": event.environment,
                "query_names": list(queries),
                "observed_values": observed_values,
            },
            confidence=0.8,
            status=EvidenceStatus(status),
        )
        return ProviderResult(
            provider=self.provider,
            status=status,
            evidence_items=[evidence],
            error_message="; ".join(failures) or None,
        )

    def _query_instant(self, query: str, query_time: float) -> JsonValue:
        endpoint = f"{self.base_url}/api/v1/query"
        url = f"{endpoint}?{urllib.parse.urlencode({'query': query, 'time': query_time})}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            status_code = response.getcode()
            if status_code >= 400:
                raise ValueError(f"Prometheus HTTP error: {status_code}")
            payload = json.loads(response.read().decode("utf-8"))

        return _observed_value(payload)


def _queries_for_event(
    event: IncidentEvent, query: PrometheusQuery | None = None
) -> dict[str, str]:
    labels = {
        "service": _escape_label_value(event.service),
        "environment": _escape_label_value(event.environment),
    }
    names = [item.value for item in query.metric_names] if query else list(_QUERY_TEMPLATES)
    if query:
        names = names[: query.limit]
    queries = {
        name: _with_aggregation(_QUERY_TEMPLATES[name], query).format(**labels)
        for name in names
    }
    if query and query.instance:
        instance = _escape_label_value(query.instance)
        queries = {
            name: value.replace("}", f',instance="{instance}"}}', 1)
            for name, value in queries.items()
        }
    return queries


def _with_aggregation(template: str, query: PrometheusQuery | None) -> str:
    if query is None or template.startswith("histogram_quantile"):
        return template
    if not template.startswith("sum("):
        return template
    aggregation = MetricAggregation(query.aggregation).value
    return f"{aggregation}({template[4:]}"


def _observed_value(payload: Any) -> JsonValue:
    if not isinstance(payload, dict):
        raise ValueError("Prometheus response must be a JSON object")

    if payload.get("status") != "success":
        error_type = payload.get("errorType", "unknown")
        error_message = payload.get("error", "unknown error")
        raise ValueError(f"Prometheus query failed: {error_type}: {error_message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Prometheus response missing data object")

    result = data.get("result")
    if not isinstance(result, list):
        raise ValueError("Prometheus response data.result must be a list")
    if not result:
        return None

    first_result = result[0]
    if not isinstance(first_result, dict):
        raise ValueError("Prometheus result entries must be JSON objects")

    value = first_result.get("value")
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("Prometheus result entry missing value pair")

    return _json_value(value[1])


def _json_value(value: Any) -> JsonValue:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if isinstance(value, int | float | bool) or value is None:
        return value
    raise ValueError("Prometheus observed value must be JSON scalar")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
