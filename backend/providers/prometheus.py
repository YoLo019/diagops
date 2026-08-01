import json
import math
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.diagnosis.signal_semantics import (
    AnomalySegment,
    SeriesPoint,
    detect_anomaly_segments,
    select_balanced_evidence,
)
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
    "network_drops": (
        'sum(rate(container_network_receive_packets_dropped_total'
        '{{container="{service}",environment="{environment}"}}[5m]))'
    ),
    "process_restarts": (
        'sum(rate(kube_pod_container_status_restarts_total'
        '{{container="{service}",environment="{environment}"}}[5m]))'
    ),
}

_SIGNAL_TYPES = {
    "qps": "traffic",
    "5xx_rate": "error",
    "p95_latency": "latency",
    "cpu": "cpu",
    "memory": "memory",
    "network_drops": "network_corruption",
    "process_restarts": "process",
}

# optional metric 查询成功但无 series 表示“无该信号”，不是 Provider failure。
_OPTIONAL_QUERIES = frozenset({"network_drops", "process_restarts"})
# step 保证每 series 约不超过 240 点；响应超限属于显式失败。
_MAX_RANGE_POINTS = 240


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
        if query:
            start, end = query.start_time, query.end_time
        else:
            window = timedelta(minutes=event.time_window_minutes)
            start, end = event.started_at - window, event.started_at + window
        # baseline 是紧邻诊断窗口、与诊断窗口等长的前置窗口（spec R2）。
        baseline_start = start - (end - start)
        step = _range_step(baseline_start, end)

        candidates: list[EvidenceItem] = []
        failures: list[str] = []
        for name, promql in queries.items():
            try:
                points = self._query_range_points(promql, baseline_start, end, step)
            except Exception as exc:
                failures.append(f"{name} range: {exc}")
                points = None
            if points is not None:
                if not points:
                    if name in _OPTIONAL_QUERIES:
                        continue
                    evidence, instant_failures = self._instant_evidence(
                        name, promql, event, start, end
                    )
                    failures.extend(instant_failures)
                    candidates.extend(evidence)
                    continue
                baseline = [point for point in points if point.timestamp < start]
                observation = [point for point in points if start <= point.timestamp <= end]
                for segment in detect_anomaly_segments(baseline, observation):
                    candidates.append(_segment_evidence(name, segment, event))
                continue
            # range 失败：保留现有 instant Evidence，但显式标记 onset 不可用，
            # 不得伪造 segment。
            evidence, instant_failures = self._instant_evidence(
                name, promql, event, start, end
            )
            failures.extend(instant_failures)
            candidates.extend(evidence)

        limit = query.limit if query else 20
        selected = select_balanced_evidence(candidates, limit)
        if not selected and failures:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message="; ".join(failures),
            )
        status = ProviderStatus.PARTIAL if failures else ProviderStatus.SUCCESS
        return ProviderResult(
            provider=self.provider,
            status=status,
            evidence_items=[
                item.model_copy(update={"status": EvidenceStatus(status)})
                for item in selected
            ],
            error_message="; ".join(failures) or None,
        )

    def _instant_evidence(
        self,
        name: str,
        promql: str,
        event: IncidentEvent,
        start: datetime,
        end: datetime,
    ) -> tuple[list[EvidenceItem], list[str]]:
        values: dict[str, float] = {}
        failures: list[str] = []
        for period, timestamp in (("current", end), ("baseline", start)):
            try:
                values[period] = _number(
                    self._query_instant(promql, timestamp.timestamp())
                )
            except Exception as exc:
                failures.append(f"{name} {period}: {exc}")
        if len(values) != 2:
            return [], failures
        current = values["current"]
        baseline = values["baseline"]
        change_percent = (current - baseline) / max(abs(baseline), 1e-9) * 100
        deviation = abs(change_percent) / 50
        return [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.METRIC_TREND,
                timestamp=end,
                summary=f"Prometheus returned {name} for {event.service}",
                payload={
                    "service": event.service,
                    "environment": event.environment,
                    "component": event.service,
                    "signal_type": _SIGNAL_TYPES[name],
                    "signal_name": name,
                    "current_value": current,
                    "baseline_value": baseline,
                    "change_percent": change_percent,
                    "deviation_score": deviation,
                    "normalized_strength": min(deviation, 10.0),
                    "onset_unavailable": True,
                    "query_names": [name],
                    "observed_values": {name: current},
                    "baseline_values": {name: baseline},
                },
                confidence=0.8,
            )
        ], []

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

    def _query_range_points(
        self, query: str, start: datetime, end: datetime, step: int
    ) -> list[SeriesPoint]:
        endpoint = f"{self.base_url}/api/v1/query_range"
        parameters = urllib.parse.urlencode(
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            }
        )
        url = f"{endpoint}?{parameters}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            status_code = response.getcode()
            if status_code >= 400:
                raise ValueError(f"Prometheus HTTP error: {status_code}")
            payload = json.loads(response.read().decode("utf-8"))

        data = _success_data(payload)
        result = data.get("result")
        if not isinstance(result, list):
            raise ValueError("Prometheus response data.result must be a list")
        if not result:
            return []
        # 模板均为 sum() 聚合，正常返回单 series；多 series 只取第一条。
        first_result = result[0]
        if not isinstance(first_result, dict):
            raise ValueError("Prometheus result entries must be JSON objects")
        values = first_result.get("values")
        if not isinstance(values, list):
            raise ValueError("Prometheus range entry missing values list")
        if len(values) > _MAX_RANGE_POINTS:
            raise ValueError(
                f"Prometheus range returned {len(values)} points"
                f" (limit {_MAX_RANGE_POINTS})"
            )
        points = []
        for entry in values:
            if not isinstance(entry, list) or len(entry) < 2:
                raise ValueError("Prometheus range value must be a [timestamp, value] pair")
            timestamp = _finite_number(entry[0], "range timestamp")
            value = _finite_number(entry[1], "range value")
            points.append(
                SeriesPoint(
                    timestamp=datetime.fromtimestamp(timestamp, UTC),
                    value=value,
                )
            )
        return points


def _segment_evidence(
    name: str, segment: AnomalySegment, event: IncidentEvent
) -> EvidenceItem:
    change_percent = (
        (segment.peak_value - segment.baseline_value)
        / max(abs(segment.baseline_value), 1e-9)
        * 100
    )
    return EvidenceItem(
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=segment.onset,
        summary=f"Prometheus detected {name} anomaly for {event.service}",
        payload={
            "service": event.service,
            "environment": event.environment,
            "component": event.service,
            "signal_type": _SIGNAL_TYPES[name],
            "signal_name": name,
            "current_value": segment.peak_value,
            "baseline_value": segment.baseline_value,
            "change_percent": change_percent,
            # 兼容字段，值已归一化到 0..10，与 normalized_strength 相同。
            "deviation_score": segment.strength,
            "normalized_strength": segment.strength,
            "anomaly_onset": segment.onset.isoformat(),
            "anomaly_ended_at": segment.ended_at.isoformat(),
            "anomaly_segment_id": segment.segment_id,
            # additive 质量字段：强度口径与 segment 支持点数，供 basis-aware 排序。
            "strength_basis": segment.strength_basis,
            "anomaly_point_count": segment.point_count,
            "query_names": [name],
            "observed_values": {name: segment.peak_value},
            "baseline_values": {name: segment.baseline_value},
        },
        confidence=0.8,
    )


def _range_step(start: datetime, end: datetime) -> int:
    span_seconds = max(1.0, (end - start).total_seconds())
    return max(1, math.ceil(span_seconds / (_MAX_RANGE_POINTS - 1)))


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


def _success_data(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Prometheus response must be a JSON object")

    if payload.get("status") != "success":
        error_type = payload.get("errorType", "unknown")
        error_message = payload.get("error", "unknown error")
        raise ValueError(f"Prometheus query failed: {error_type}: {error_message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Prometheus response missing data object")
    return data


def _observed_value(payload: Any) -> JsonValue:
    data = _success_data(payload)

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


def _number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Prometheus observed value must be numeric")
    return float(value)


def _finite_number(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Prometheus {label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Prometheus {label} must be finite")
    return parsed


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
