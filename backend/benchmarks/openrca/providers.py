from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, median

from backend.benchmarks.openrca.models import OpenRcaRuntimeCase
from backend.benchmarks.openrca.telemetry import (
    resolve_case_directory,
    row_component,
    row_duration,
    row_metrics,
    row_parent_span_id,
    row_span_id,
    row_success,
    row_timestamp,
    row_trace_id,
    rows_for,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import (
    DependencyDirection,
    DependencyQuery,
    LogQuery,
    MetricAggregation,
    MetricQuery,
)
from backend.providers.results import ProviderResult, ProviderStatus


class _OpenRcaProvider:
    def __init__(self, dataset_root: Path, case: OpenRcaRuntimeCase) -> None:
        self.case = case
        self.directory = resolve_case_directory(dataset_root, case.telemetry_dir)

    @staticmethod
    def _window(event: IncidentEvent, query) -> tuple[datetime, datetime, int]:
        if query is not None:
            return query.start_time, query.end_time, query.limit
        window = timedelta(minutes=event.time_window_minutes)
        return event.started_at - window, event.started_at + window, 20


class OpenRcaLogProvider(_OpenRcaProvider):
    provider = EvidenceProvider.LOG
    supported_tools = frozenset({"read_logs"})

    def collect(
        self, event: IncidentEvent, query: LogQuery | None = None
    ) -> ProviderResult:
        start, end, limit = self._window(event, query)
        keywords = [item.casefold() for item in (query.keywords if query else [])]
        levels = {item.casefold() for item in (query.levels if query else [])}
        instance = query.instance if query else None
        matches: list[dict[str, str]] = []
        malformed = 0
        for row in rows_for(self.directory, "log"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            component = row_component(row)
            level = (row.get("level") or row.get("severity") or "").strip()
            message = (
                row.get("message") or row.get("log") or row.get("content") or ""
            ).strip()
            if timestamp is None or not message:
                malformed += 1
                continue
            row_instance = row.get("instance", "").strip()
            searchable = f"{component or ''} {message}".casefold()
            if (
                timestamp is None
                or not start <= timestamp <= end
                or (instance and row_instance != instance)
                or (levels and level.casefold() not in levels)
                or (keywords and not all(item in searchable for item in keywords))
            ):
                continue
            matches.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "component": component or "unknown",
                    "level": level,
                    "message": message[:500],
                    "instance": row_instance,
                }
            )
            if len(matches) >= limit:
                break
        if not matches:
            return _empty_result(self.provider, malformed)
        claims = [
            {
                "component": item["component"],
                "reason": f"log error: {item['message'][:120]}",
                "occurred_at": item["timestamp"],
            }
            for item in matches
            if item["level"].casefold() in {"error", "critical", "fatal"}
        ]
        payload = {"matches": matches, "root_cause_claims": claims}
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=[
                _evidence(
                    self.case,
                    self.provider,
                    EvidenceKind.LOG_PATTERN,
                    datetime.fromisoformat(matches[-1]["timestamp"]),
                    f"{len(matches)} bounded log matches",
                    payload,
                )
            ],
            error_message=_malformed_warning(malformed),
        )


class OpenRcaMetricProvider(_OpenRcaProvider):
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})

    def collect(
        self, event: IncidentEvent, query: MetricQuery | None = None
    ) -> ProviderResult:
        start, end, limit = self._window(event, query)
        requested = set(query.metric_names if query else [])
        requested_instance = query.instance if query else None
        points: list[dict[str, object]] = []
        malformed = 0
        for row in rows_for(self.directory, "metric", "kpi"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            component = row_component(row)
            instance = row.get("instance", "").strip()
            if timestamp is None or component is None:
                malformed += 1
                continue
            metrics = list(row_metrics(row))
            if not metrics:
                malformed += 1
                continue
            for metric_name, value in metrics:
                if requested and metric_name not in requested:
                    continue
                if requested_instance and instance != requested_instance:
                    continue
                points.append(
                    {
                        "timestamp": timestamp,
                        "component": component,
                        "instance": instance,
                        "metric_name": metric_name,
                        "value": value,
                    }
                )
        groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for point in points:
            if point["timestamp"].date() == self.case.start_time.date():
                groups[
                    (
                        str(point["component"]),
                        str(point["instance"]),
                        str(point["metric_name"]),
                    )
                ].append(float(point["value"]))

        anomalies: list[dict[str, object]] = []
        for point in points:
            timestamp = point["timestamp"]
            if not start <= timestamp <= end:
                continue
            key = (
                str(point["component"]),
                str(point["instance"]),
                str(point["metric_name"]),
            )
            values = groups[key]
            baseline = median(values)
            mad = median(abs(value - baseline) for value in values)
            threshold = max(3 * mad, abs(baseline) * 0.1, 1e-9)
            if abs(float(point["value"]) - baseline) <= threshold:
                continue
            anomalies.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "component": point["component"],
                    "instance": point["instance"],
                    "metric_name": point["metric_name"],
                    "value": point["value"],
                    "median": baseline,
                    "mad": mad,
                }
            )
        anomalies.sort(key=lambda item: str(item["timestamp"]))
        anomalies = anomalies[:limit]
        if not anomalies:
            return _empty_result(self.provider, malformed)
        claims = [
            {
                "component": str(item["component"]),
                "reason": f"metric anomaly: {item['metric_name']}",
                "occurred_at": str(item["timestamp"]),
            }
            for item in anomalies
        ]
        aggregation = query.aggregation if query else MetricAggregation.AVG
        aggregate_values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for item in anomalies:
            aggregate_values[
                (str(item["component"]), str(item["metric_name"]))
            ].append(float(item["value"]))
        aggregates = [
            {
                "component": component,
                "metric_name": metric_name,
                "value": _aggregate(values, aggregation),
            }
            for (component, metric_name), values in sorted(aggregate_values.items())
        ]
        payload = {
            "aggregation": aggregation.value,
            "aggregates": aggregates,
            "anomalies": anomalies,
            "root_cause_claims": claims,
        }
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=[
                _evidence(
                    self.case,
                    self.provider,
                    EvidenceKind.METRIC_TREND,
                    datetime.fromisoformat(str(anomalies[-1]["timestamp"])),
                    f"{len(anomalies)} bounded metric anomalies",
                    payload,
                )
            ],
            error_message=_malformed_warning(malformed),
        )


class OpenRcaDependencyProvider(_OpenRcaProvider):
    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})

    def collect(
        self, event: IncidentEvent, query: DependencyQuery | None = None
    ) -> ProviderResult:
        start, end, limit = self._window(event, query)
        spans: dict[tuple[str, str], dict[str, object]] = {}
        malformed = 0
        for row in rows_for(self.directory, "trace", "span"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            trace_id = row_trace_id(row)
            span_id = row_span_id(row)
            component = row_component(row)
            duration = row_duration(row)
            if (
                timestamp is None
                or not start <= timestamp <= end
                or trace_id is None
                or span_id is None
                or component is None
                or duration is None
            ):
                malformed += 1
                continue
            spans[(trace_id, span_id)] = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_id": row_parent_span_id(row),
                "component": component,
                "timestamp": timestamp,
                "duration": duration,
                "success": row_success(row),
            }

        aggregates: dict[tuple[str, str], dict[str, object]] = {}
        for span in spans.values():
            parent_id = span["parent_id"]
            parent = spans.get((str(span["trace_id"]), str(parent_id)))
            if parent is None:
                continue
            parent_component = str(parent["component"])
            child_component = str(span["component"])
            if query and query.target and query.target not in {
                parent_component,
                child_component,
            }:
                continue
            if query and query.direction == DependencyDirection.UPSTREAM:
                parent_component, child_component = child_component, parent_component
            key = (parent_component, child_component)
            aggregate = aggregates.setdefault(
                key,
                {
                    "parent": parent_component,
                    "child": child_component,
                    "latency_total": 0.0,
                    "count": 0,
                    "error_count": 0,
                    "timestamps": [],
                },
            )
            aggregate["latency_total"] += float(span["duration"])
            aggregate["count"] += 1
            aggregate["error_count"] += int(not bool(span["success"]))
            aggregate["timestamps"].append(span["timestamp"])

        edge_claims = []
        for aggregate in aggregates.values():
            timestamps = aggregate.pop("timestamps")
            latency_total = aggregate.pop("latency_total")
            edge = {
                **aggregate,
                "latency": latency_total / int(aggregate["count"]),
            }
            claim = None
            if edge["error_count"]:
                claim = {
                    "component": edge["child"],
                    "reason": "dependency errors",
                    "occurred_at": min(timestamps).isoformat(),
                }
            edge_claims.append((edge, claim))
        edge_claims.sort(
            key=lambda item: (str(item[0]["parent"]), str(item[0]["child"]))
        )
        edge_claims = edge_claims[:limit]
        edges = [item[0] for item in edge_claims]
        claims = [item[1] for item in edge_claims if item[1] is not None]
        if not edges:
            return _empty_result(self.provider, malformed)
        payload = {"edges": edges, "root_cause_claims": claims}
        latest = max(span["timestamp"] for span in spans.values())
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=[
                _evidence(
                    self.case,
                    self.provider,
                    EvidenceKind.DEPENDENCY_HEALTH,
                    latest,
                    f"{len(edges)} bounded dependency edges",
                    payload,
                )
            ],
            error_message=_malformed_warning(malformed),
        )


def _evidence(
    case: OpenRcaRuntimeCase,
    provider: EvidenceProvider,
    kind: EvidenceKind,
    timestamp: datetime,
    summary: str,
    payload: dict,
) -> EvidenceItem:
    fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    evidence_id = hashlib.sha256(
        f"{case.case_id}:{provider.value}:{fingerprint}".encode()
    ).hexdigest()[:24]
    return EvidenceItem(
        id=f"ev-openrca-{evidence_id}",
        provider=provider,
        kind=kind,
        timestamp=timestamp,
        summary=summary,
        payload=payload,
    )


def _aggregate(values: list[float], aggregation: MetricAggregation) -> float:
    if aggregation == MetricAggregation.MAX:
        return max(values)
    if aggregation == MetricAggregation.SUM:
        return sum(values)
    return fmean(values)


def _status(malformed: int) -> ProviderStatus:
    return ProviderStatus.PARTIAL if malformed else ProviderStatus.SUCCESS


def _malformed_warning(malformed: int) -> str | None:
    return f"ignored {malformed} malformed telemetry rows" if malformed else None


def _empty_result(
    provider: EvidenceProvider, malformed: int
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=_status(malformed),
        error_message=_malformed_warning(malformed),
    )
