"""RCAEval 匿名遥测包的只读 Provider 适配器。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median
from threading import Lock
from types import MappingProxyType

from backend.diagnosis.signal_semantics import classify_metric_signal, signal_family_priority
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvenance,
    EvidenceProvider,
    EvidenceScope,
    EvidenceSourceClass,
    RuntimeKind,
    RuntimeStatePayload,
    RuntimeStateValue,
    SpanStatus,
    TraceSpanPayload,
)
from backend.domain.tool_queries import (
    DependencyQuery,
    LogQuery,
    MetricQuery,
    RuntimeStateQuery,
    ServiceCatalogQuery,
    TraceQuery,
)
from backend.providers.local_package import expand_span_selection
from backend.providers.results import ProviderResult, ProviderStatus
from backend.providers.trace_timing import with_child_timing
from backend.safety.redaction import assert_safe_value, redact_text, redact_value

ADAPTER_VERSION = "rcaeval-re2-v8"
_MAX_SCAN_ROWS = 250_000
_MAX_TRACE_SCAN_ROWS = 1_000_000
_METRIC_QUERY_ALIASES = {
    "cpu_usage": "cpu", "memory": "mem", "memory_usage": "mem",
    "disk_io": "diskio", "disk_i/o": "diskio", "connections": "socket",
    "connection_count": "socket",
}
_SIGNAL_METRIC_PATTERN = re.compile(
    r"_(?:cpu|mem|diskio|socket|workload|error|latency-(?:50|90|95|99))$",
    re.IGNORECASE,
)


def _metric_query_matches(name: str, requested: str) -> bool:
    name = name.casefold()
    if requested in name:
        return True
    for alias, canonical in _METRIC_QUERY_ALIASES.items():
        if requested == alias:
            return name.endswith("_" + canonical)
        if requested.endswith("_" + alias):
            return name == requested[:-len(alias)] + canonical
    return False


class _Telemetry:
    def __init__(self, case_dir: Path) -> None:
        self.case_dir = case_dir.resolve()
        self.files = tuple(sorted(self.case_dir.glob("telemetry-*")))
        self._headers: dict[Path, list[str]] = {}
        self._trace_rows = None
        self.trace_scan_truncated = False
        self.service_aliases = {}
        self._trace_lock = Lock()
        for path in self.files:
            if path.suffix.casefold() != ".csv":
                continue
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                self._headers[path] = next(csv.reader(handle), [])

    def trace_rows(self):
        """冻结遥测包在同次运行共享有界快照，避免并发反复解析同一大文件。"""
        with self._trace_lock:
            if self._trace_rows is None:
                rows = []
                keys = ("time", "startTimeMillis", "traceID", "spanID", "parentSpanID",
                        "serviceName", "operationName", "methodName", "duration", "statusCode")
                for path in self.trace_files:
                    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                        for index, row in enumerate(csv.DictReader(handle)):
                            if len(rows) >= _MAX_TRACE_SCAN_ROWS:
                                self.trace_scan_truncated = True
                                break
                            rows.append((f"{path.name}:{index}", MappingProxyType(
                                {key: row[key] for key in keys if key in row}
                            )))
                    if self.trace_scan_truncated:
                        break
                metric_services = {_entity_from_metric(name) for path in self.metric_files
                                   for name in self._headers[path][1:] if _is_signal_metric(name)}
                trace_services = {row.get("serviceName", "") for _, row in rows}
                # 仅消解源中无歧义的 service 后缀差异；两种名字同时存在时绝不合并。
                self.service_aliases = {
                    name: name[:-7] for name in trace_services
                    if name.endswith("service") and name not in metric_services
                    and name[:-7] in metric_services and name[:-7] not in trace_services
                }
                self._trace_rows = tuple(rows)
            return self._trace_rows

    @property
    def log_files(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path, header in self._headers.items()
            if {"container_name", "message"} <= set(header)
        )

    @property
    def trace_files(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path, header in self._headers.items()
            if {"traceID", "spanID", "serviceName"} <= set(header)
        )

    @property
    def pod_files(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path, header in self._headers.items()
            if {"POD", "NODE_NAME"} <= set(header)
        )

    @property
    def metric_files(self) -> tuple[Path, ...]:
        excluded = set(self.log_files) | set(self.trace_files) | set(self.pod_files)
        return tuple(
            path
            for path, header in self._headers.items()
            if path not in excluded and header and header[0].casefold() == "time"
        )


class _RcaEvalProvider:
    def __init__(
        self,
        case_dir: Path,
        *,
        case_id: str,
        runtime_manifest_hash: str,
        evidence_namespace: str,
    ) -> None:
        self.telemetry = _Telemetry(case_dir)
        self.case_id = case_id
        self.runtime_manifest_hash = runtime_manifest_hash
        self.evidence_namespace = evidence_namespace

    def _evidence(
        self,
        *,
        provider: EvidenceProvider,
        kind: EvidenceKind,
        timestamp: datetime,
        summary: str,
        payload: dict,
        entity_ids: list[str] | None = None,
        identity: str | None = None,
    ) -> EvidenceItem:
        safe_summary = redact_text(summary)[:512]
        safe_payload = redact_value(payload)
        assert_safe_value(safe_summary)
        assert_safe_value(safe_payload)
        canonical = json.dumps(
            [
                self.evidence_namespace,
                self.case_id,
                provider.value,
                kind.value,
                safe_summary,
                safe_payload,
                identity,
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EvidenceItem(
            id=f"ev-{hashlib.sha256(canonical).hexdigest()[:32]}",
            provider=provider,
            kind=kind,
            timestamp=timestamp,
            summary=safe_summary,
            payload=safe_payload,
            scope=EvidenceScope(entity_ids=entity_ids or [], observed_at=timestamp),
            provenance=EvidenceProvenance(
                source_class=EvidenceSourceClass.PUBLIC_DATASET,
                provider_profile="offline",
                source_artifact_id=self.case_id,
                source_artifact_hash=self.runtime_manifest_hash,
                adapter_version=ADAPTER_VERSION,
            ),
            runtime_run_id=self.evidence_namespace,
        )


class RcaEvalLogProvider(_RcaEvalProvider):
    provider = EvidenceProvider.LOG
    supported_tools = frozenset({"read_logs"})

    def collect(self, event: IncidentEvent, query: LogQuery | None = None) -> ProviderResult:
        keywords = [value.casefold() for value in (query.keywords if query else [])]
        levels = {value.casefold() for value in (query.levels if query else [])}
        instance = query.instance.casefold() if query and query.instance else None
        limit = query.limit if query else 20
        evidence: list[EvidenceItem] = []
        truncated = False
        preview = {}
        buckets = {}
        first_matches = []
        matching_count = 0
        matched_start = matched_end = None
        scan_incomplete = False
        for path in self.telemetry.log_files:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for row_index, row in enumerate(csv.DictReader(handle)):
                    if row_index >= _MAX_SCAN_ROWS:
                        truncated = True
                        scan_incomplete = True
                        break
                    service = (row.get("container_name") or "unknown").strip()
                    message = (row.get("message") or "").strip()
                    level = (row.get("level") or "").strip()
                    error = redact_text((row.get("error") or "").strip())[:500]
                    searchable = f"{service} {message} {error}".casefold()
                    if not message or (
                        keywords and not all(item in searchable for item in keywords)
                    ):
                        continue
                    if levels and level.casefold() not in levels:
                        continue
                    if instance and instance not in service.casefold():
                        continue
                    timestamp = _timestamp(row, event.started_at)
                    if query and not query.start_time <= timestamp < query.end_time:
                        continue
                    if query is None:
                        key = (service, level, message, error, f"{path.name}:{row_index}")
                        priority = (int(bool(error)) + int(level.casefold() in {
                            "error", "critical", "warning", "warn",
                        }), -sum(item[:4] == key[:4] for item in preview))
                        if len(preview) >= limit:
                            truncated = True
                            lowest = min(preview, key=lambda k: preview[k][0])
                            if preview[lowest][0] >= priority:
                                continue
                            del preview[lowest]
                        preview[key] = (priority, timestamp, f"{path.name}:{row_index}")
                        continue
                    matching_count += 1
                    matched_start = min(matched_start, timestamp) if matched_start else timestamp
                    matched_end = max(matched_end, timestamp) if matched_end else timestamp
                    record = (service, level, redact_text(message)[:500], error,
                              timestamp, f"{path.name}:{row_index}")
                    if len(first_matches) < limit:
                        first_matches.append(record)
                    # 对整个查询窗有界扫描，每个时间桶优先错误、同级取最新。
                    # 不按文件顺序截断，避免高频故障前日志遮住故障期观测。
                    bucket = min(limit - 1, int(
                        (timestamp - query.start_time).total_seconds()
                        / (query.end_time - query.start_time).total_seconds() * limit
                    ))
                    priority = (int(bool(error)) + int(level.casefold() in {
                        "error", "critical", "warning", "warn",
                    }), timestamp)
                    if bucket not in buckets or priority > buckets[bucket][0]:
                        buckets[bucket] = (priority, record)
        if query is None:
            for (service, level, message, error, _), (_, timestamp, identity) in sorted(
                preview.items(), key=lambda item: item[1][0], reverse=True,
            ):
                evidence.append(self._evidence(
                    provider=self.provider, kind=EvidenceKind.LOG_PATTERN, timestamp=timestamp,
                    summary=f"log signal on {service}: {redact_text(message)[:150]}"
                            + (f"; error: {error[:280]}" if error else ""),
                    payload={"service": service[:128], "level": level[:32],
                             "message": redact_text(message)[:500],
                             **({"error": error} if error else {})},
                    entity_ids=[service[:128]], identity=identity,
                ))
        else:
            truncated = scan_incomplete or matching_count > limit
            selected = {record[-1]: record for _, record in buckets.values()}
            for record in first_matches:
                if len(selected) >= limit:
                    break
                selected.setdefault(record[-1], record)
            sampling = {
                "selection": "time_bucket_error_priority" if truncated else "all_matches",
                "matched_start": matched_start.isoformat() if matched_start else None,
                "matched_end": matched_end.isoformat() if matched_end else None,
                "matching_count": matching_count,
                "scan_complete": not scan_incomplete,
            }
            for service, level, message, error, timestamp, identity in sorted(
                selected.values(), key=lambda record: (record[4], record[5]),
            ):
                evidence.append(self._evidence(
                    provider=self.provider, kind=EvidenceKind.LOG_PATTERN, timestamp=timestamp,
                    summary=f"log signal on {service}: {message[:150]}"
                            + (f"; error: {error[:280]}" if error else ""),
                    payload={"service": service[:128], "level": level[:32],
                             "message": message, "sampling": sampling,
                             **({"error": error} if error else {})},
                    entity_ids=[service[:128]], identity=identity,
                ))
        return ProviderResult(
            provider=self.provider,
            status=ProviderStatus.PARTIAL if truncated else ProviderStatus.SUCCESS,
            evidence_items=evidence,
            truncated=truncated,
            returned_count=len(evidence),
            error_message=("source_scan_incomplete: row scan limit reached" if scan_incomplete
                           else "query_result_truncated: representative sample; narrow time window"
                           if truncated else None),
        )


class RcaEvalMetricProvider(_RcaEvalProvider):
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})

    def collect(
        self, event: IncidentEvent, query: MetricQuery | None = None
    ) -> ProviderResult:
        requested = [item.casefold() for item in (query.metric_names if query else [])]
        instance = query.instance.casefold() if query and query.instance else None
        # 首次采集要给 Investigator 一个可查询的指标面；过小的 top-k 会
        # 把 socket/disk/latency 等低幅度但有区分度的信号永久截掉。
        limit = query.limit if query else 60
        has_signal_columns = any(
            _is_signal_metric(name)
            for path in self.telemetry.metric_files
            for name in self.telemetry._headers[path][1:]
        )
        changes: list[tuple[float, str, str, float, float, datetime, str]] = []
        statistics = {}
        metric_names = [
            name for path in self.telemetry.metric_files
            for name in self.telemetry._headers[path][1:]
        ]
        aggregation = query.aggregation.value if query else "avg"
        aggregate = {"avg": fmean, "max": max, "sum": sum}[aggregation]
        primary_names = set()
        candidates = [name for name in metric_names if not instance or instance in name.casefold()]
        if requested:
            for token in requested:
                matches = [name for name in candidates if _metric_query_matches(name, token)]
                preferred = [name for name in matches
                             if name.casefold() == token or _is_signal_metric(name)]
                primary_names.update(preferred or matches)
        else:
            primary_names.update(name for name in candidates
                                 if not has_signal_columns or _is_signal_metric(name))
        # 同窗口的细项直接与汇总一起呈现，避免只见总量而漏掉组成；不作故障判断。
        detail_names = {detail for name in primary_names
                        for detail in _related_metric_names(name, metric_names)}
        by_name = {}
        for path in self.telemetry.metric_files:
            header = self.telemetry._headers[path]
            selected = [(index, name) for index, name in enumerate(header[1:], start=1)
                        if name in primary_names or name in detail_names][:400]
            if not selected:
                continue
            series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                for row_index, row in enumerate(reader):
                    if row_index >= _MAX_SCAN_ROWS:
                        break
                    timestamp = _cell_timestamp(row[0] if row else "", event.started_at)
                    if query and not query.start_time <= timestamp < query.end_time:
                        continue
                    for index, name in selected:
                        if index >= len(row):
                            continue
                        value = _finite_float(row[index])
                        if value is not None:
                            series[name].append((timestamp, value))
            for name, points in series.items():
                if len(points) < 4:
                    continue
                points.sort(key=lambda point: point[0])
                middle = len(points) // 2
                before = [value for _, value in points[:middle]]
                after = [value for _, value in points[middle:]]
                baseline = aggregate(before)
                observation = aggregate(after)
                identity = f"{path.name}:{name}"
                sample_gaps = [(right[0] - left[0]).total_seconds()
                               for left, right in zip(points, points[1:], strict=False)]
                statistics[identity] = {
                    "baseline_mean": fmean(before), "observation_mean": fmean(after),
                    "aggregation": aggregation,
                    "baseline_value": baseline, "observation_value": observation,
                    "window_start": points[0][0].isoformat(),
                    "window_end": points[-1][0].isoformat(),
                    "split_at": points[middle][0].isoformat(), "sample_count": len(points),
                    "sampling_interval_seconds": median(sample_gaps),
                    "max_sample_gap_seconds": max(sample_gaps),
                    "baseline_min": min(before), "baseline_max": max(before),
                    "observation_min": min(after), "observation_max": max(after),
                    "time_profile": _time_profile(points),
                    "timestamp_semantics": "comparison split, not an observed failure onset",
                    "first_sustained_deviation": _first_sustained_deviation(points),
                    "related_metric_names": _related_metric_names(name, metric_names),
                    "value_semantics": "source samples; units and counter preprocessing "
                    "are not declared by this source; do not infer a rate from the name alone",
                }
                by_name[name] = statistics[identity]
                if name not in primary_names:
                    continue
                scale = max(abs(baseline), _mean_absolute_delta(before), 1e-9)
                score = abs(observation - baseline) / scale
                changes.append(
                    (
                        score,
                        name,
                        _entity_from_metric(name),
                        baseline,
                        observation,
                        points[middle][0],
                        identity,
                    )
                )
        for stats in statistics.values():
            stats["related_observations"] = [
                {"metric": name, "baseline_mean": detail["baseline_mean"],
                 "observation_mean": detail["observation_mean"],
                 "first_sustained_deviation": detail["first_sustained_deviation"].get("timestamp")}
                for name in stats["related_metric_names"]
                if (detail := by_name.get(name)) is not None
                and all(detail[key] == stats[key] for key in
                        ("window_start", "window_end", "split_at", "sample_count"))
            ]
        # 幅度仅在家族内排序；轮转覆盖避免大量延迟分位数挤掉资源信号。
        families = defaultdict(list)
        for change in sorted(changes, reverse=True):
            families[classify_metric_signal(change[1])].append(change)
        ranked_changes = sorted(
            ((index, family, change)
             for family, items in families.items()
             for index, change in enumerate(items)),
            key=lambda item: (item[0], signal_family_priority(item[1])),
        )
        selected_changes = ranked_changes[:limit]
        evidence = [
            self._evidence(
                provider=self.provider,
                kind=EvidenceKind.METRIC_TREND,
                timestamp=timestamp,
                summary=(
                    f"{metric[:180]} {aggregation} changed on {entity}: baseline={baseline:.6g}, "
                    f"observation={observation:.6g}, change_score={score:.3f}"
                ),
                payload={
                    "entity": entity,
                    "metric": metric[:256],
                    "signal_type": classify_metric_signal(metric),
                    **statistics[identity],
                    "change_score": score,
                },
                entity_ids=[entity],
                identity=identity,
            )
            for _, _, (score, metric, entity, baseline, observation, timestamp, identity)
            in selected_changes
        ]
        return _result(self.provider, evidence, truncated=len(ranked_changes) > limit)


class RcaEvalTraceProvider(_RcaEvalProvider):
    provider = EvidenceProvider.TRACE
    supported_tools = frozenset({"query_traces"})

    def collect(self, event: IncidentEvent, query: TraceQuery | None = None) -> ProviderResult:
        limit = query.limit if query else 50
        anchors = []
        for identity, row in self._rows():
            service = (row.get("serviceName") or "unknown").strip()
            operation = (row.get("operationName") or row.get("methodName") or "unknown").strip()
            if query and (
                (query.entity_ids and service not in {
                    self.telemetry.service_aliases.get(name, name) for name in query.entity_ids})
                or (query.service and service != self.telemetry.service_aliases.get(
                    query.service, query.service))
                or (query.trace_id and (row.get("traceID") or "").casefold() != query.trace_id)
                or (query.operation and query.operation not in operation)
                or (query.error_only
                    and _row_span_status(row) != SpanStatus.ERROR)
            ):
                continue
            duration = _trace_duration_ms(row)
            if query and query.min_duration_ms is not None and duration < query.min_duration_ms:
                continue
            timestamp = _trace_timestamp(row, event.started_at)
            if query and query.window_start is not None and not (
                query.window_start <= timestamp < query.window_end
            ):
                continue
            # 只为最终样本校验完整对象；宽查询不再构造数十万个 Pydantic 对象。
            anchors.append((identity, row, timestamp, duration))
        ordered = sorted(anchors, key=lambda item: (item[2], item[0]))
        slow = sorted(ordered, key=lambda item: -item[3])[:max(1, limit // 2)]
        seen = {item[0] for item in slow}
        rest = [item for item in ordered if item[0] not in seen]
        count = min(limit - len(slow), len(rest))
        raw_sample = slow + [rest[index * len(rest) // count] for index in range(count)]
        sampled = []
        checked = set()
        for identity, row, _, _ in raw_sample + ordered:
            if identity in checked:
                continue
            checked.add(identity)
            span = _parse_trace_span(row, event.started_at)
            if span is not None:
                sampled.append((identity, span))
            if len(sampled) == limit:
                break
        selected = list(sampled)
        grouped = defaultdict(list)
        if sampled:
            traces = {span.trace_id for _, span in sampled}
            grouped = defaultdict(list)
            identities = {}
            for identity, row in self._rows():
                if (row.get("traceID") or "").casefold() not in traces:
                    continue
                span = _parse_trace_span(row, event.started_at)
                if span is not None and _trace_in_window(span, query):
                    grouped[span.trace_id].append(span)
                    identities[(span.trace_id, span.span_id)] = identity
            selected = []
            seen_spans = set()
            for identity, anchor in sampled:
                # 必须按 trace 分组，span ID 不保证跨 trace 唯一。
                expanded = expand_span_selection(
                    grouped[anchor.trace_id], [anchor], query.direction
                ) if query and (query.service or query.operation or query.entity_ids) else []
                for span in [anchor] + expanded:
                    key = (span.trace_id, span.span_id)
                    if key not in seen_spans:
                        selected.append((identities.get(key, identity), span))
                        seen_spans.add(key)
        truncated = (self.telemetry.trace_scan_truncated
                     or len(anchors) > len(sampled) or len(selected) > limit)
        enriched = {(span.trace_id, span.span_id): span
                    for trace in grouped.values() for span in with_child_timing(trace)}
        selected = [(identity, _with_rpc_peer(
            enriched.get((span.trace_id, span.span_id), span), grouped[span.trace_id],
        )) for identity, span in selected]
        evidence = [self._evidence(
            provider=self.provider,
            kind=EvidenceKind.TRACE_ERROR if span.status == SpanStatus.ERROR
                 else EvidenceKind.TRACE_LATENCY,
            timestamp=span.started_at,
            summary=f"trace {span.status.value} on {span.service}: "
                    f"{span.operation} {span.duration_ms:.3f}ms"
                    + (f"; gRPC status={span.attributes['rpc.status_code']}"
                       if "rpc.status_code" in span.attributes else "")
                    + (f"; same-RPC {span.attributes['rpc.peer_role']} "
                       f"{span.attributes['rpc.peer_service']} "
                       f"{span.attributes['rpc.peer_duration_ms']}ms"
                       if "rpc.peer_service" in span.attributes else ""),
            payload=span.model_dump(mode="json"),
            entity_ids=[span.service], identity=identity,
        ) for identity, span in selected[:limit]]
        return ProviderResult(
            provider=self.provider,
            status=ProviderStatus.PARTIAL if truncated else ProviderStatus.SUCCESS,
            evidence_items=evidence, truncated=truncated, returned_count=len(evidence),
            error_message=("source_scan_incomplete: source row limit reached; empty results "
                           "do not establish absence in the requested window")
            if self.telemetry.trace_scan_truncated else (
                "query_result_truncated: duration/time sample, not a distribution; "
                "narrow query filters" if truncated else None),
        )

    def _rows(self):
        for identity, row in self.telemetry.trace_rows():
            original = row.get("serviceName", "")
            canonical = self.telemetry.service_aliases.get(original)
            yield identity, ({**row, "serviceName": canonical, "source_service": original}
                             if canonical else row)


class RcaEvalDependencyProvider(RcaEvalTraceProvider):
    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})

    def collect(
        self, event: IncidentEvent, query: DependencyQuery | None = None
    ) -> ProviderResult:
        limit = query.limit if query else 20
        spans = {}
        for _source_identity, row in self._rows():
            trace_id = (row.get("traceID") or "").casefold()
            span_id = (row.get("spanID") or "").casefold()
            service = (row.get("serviceName") or "unknown")[:128]
            if trace_id and span_id:
                spans[(trace_id, span_id)] = (
                    service,
                    (row.get("parentSpanID") or None),
                    _trace_timestamp(row, event.started_at),
                    _trace_duration_ms(row),
                    _row_span_status(row),
                    (row.get("operationName") or "").lstrip("/"),
                )
        edges: dict[tuple[str, str], list[tuple[datetime, float, SpanStatus]]] = defaultdict(list)
        paired = defaultdict(list)
        for (trace_id, _), values in spans.items():
            child, parent_id, timestamp, duration, status, operation = values
            parent = spans.get((trace_id, parent_id or ""))
            if parent is None or parent[0] == child:
                continue
            source, target = parent[0], child
            if query:
                if not query.start_time <= timestamp < query.end_time:
                    continue
                scoped_entity = source if query.direction.value == "downstream" else target
                if query.target and self.telemetry.service_aliases.get(
                    query.target, query.target
                ) != scoped_entity:
                    continue
            edges[(source, target)].append((timestamp, duration, status))
            if operation and operation == parent[5]:
                paired[(source, target)].append((parent[3], duration, parent[4]))
        evidence = []
        ranked_edges = sorted(
            edges.items(),
            key=lambda item: (sum(row[2] == SpanStatus.ERROR for row in item[1]), len(item[1])),
            reverse=True,
        )
        for (source, target), samples in ranked_edges[:limit]:
            errors = sum(row[2] == SpanStatus.ERROR for row in samples)
            unknown = sum(row[2] == SpanStatus.UNSET for row in samples)
            timestamp = max(row[0] for row in samples)
            latency = fmean(row[1] for row in samples)
            pair_samples = paired[(source, target)]
            pair_facts = ({
                "paired_calls": len(pair_samples),
                "caller_mean_duration_ms": fmean(row[0] for row in pair_samples),
                "callee_mean_duration_ms": fmean(row[1] for row in pair_samples),
                "caller_errors": sum(row[2] == SpanStatus.ERROR for row in pair_samples),
                "caller_unknown_status": sum(row[2] == SpanStatus.UNSET for row in pair_samples),
            } if pair_samples else {})
            evidence.append(
                self._evidence(
                    provider=self.provider,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=timestamp,
                    summary=(
                        f"dependency {source} -> {target}: calls={len(samples)}, "
                        "whole-window aggregate (not incident-only); "
                        f"callee_errors={errors}, callee_unknown={unknown}, "
                        f"callee_mean_duration_ms={latency:.3f}"
                        + (f"; paired caller_mean_ms={pair_facts['caller_mean_duration_ms']:.3f}, "
                           f"callee_mean_ms={pair_facts['callee_mean_duration_ms']:.3f}, "
                           f"caller_errors={pair_facts['caller_errors']}, "
                           f"caller_unknown={pair_facts['caller_unknown_status']}; "
                           "paired spans only, missing callees not counted" if pair_facts else "")
                    ),
                    payload={
                        "source": source,
                        "target": target,
                        "calls": len(samples),
                        "errors": errors,
                        "callee_unknown_status": unknown,
                        "mean_duration_ms": latency,
                        "window_start": (query.start_time if query else min(
                            row[0] for row in samples
                        )).isoformat(),
                        "window_end": (query.end_time if query else timestamp).isoformat(),
                        "aggregation_semantics": "all matched spans in the selected interval; "
                                                 "baseline and incident may be mixed",
                        **pair_facts,
                    },
                    entity_ids=[source, target],
                )
            )
        result = _result(self.provider, evidence,
                         truncated=len(ranked_edges) > limit or self.telemetry.trace_scan_truncated)
        if self.telemetry.trace_scan_truncated:
            result.error_message = "source_scan_incomplete: dependency coverage is incomplete"
        return result


class RcaEvalServiceCatalogProvider(RcaEvalTraceProvider):
    provider = EvidenceProvider.SERVICE_CATALOG
    supported_tools = frozenset({"read_service_catalog"})

    def collect(
        self, event: IncidentEvent, query: ServiceCatalogQuery | None = None
    ) -> ProviderResult:
        services: set[str] = set()
        for _source_identity, row in self._rows():
            service = (row.get("serviceName") or "").strip()
            if service and (not query or not query.name or query.name == service):
                services.add(service[:128])
            if len(services) >= (query.limit if query else 100):
                break
        evidence = [
            self._evidence(
                provider=self.provider,
                kind=EvidenceKind.SERVICE_METADATA,
                timestamp=event.started_at,
                summary=f"service catalog contains {service}",
                payload={"service": service, "source": "trace-serviceName"},
                entity_ids=[service],
            )
            for service in sorted(services)
        ]
        result = _result(self.provider, evidence, truncated=self.telemetry.trace_scan_truncated)
        if self.telemetry.trace_scan_truncated:
            result.error_message = "source_scan_incomplete: service catalog coverage is incomplete"
        return result


class RcaEvalRuntimeStateProvider(_RcaEvalProvider):
    provider = EvidenceProvider.RUNTIME_STATE
    supported_tools = frozenset({"read_runtime_state"})

    def collect(
        self, event: IncidentEvent, query: RuntimeStateQuery | None = None
    ) -> ProviderResult:
        limit = query.limit if query else 50
        evidence: list[EvidenceItem] = []
        for path in self.telemetry.pod_files:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for row_index, row in enumerate(csv.DictReader(handle)):
                    pod = (row.get("POD") or "").strip()[:128]
                    node = (row.get("NODE_NAME") or "").strip()[:128]
                    if not pod or (query and query.entity_ids and pod not in query.entity_ids):
                        continue
                    state = _runtime_state_value(row)
                    ready = _runtime_ready_value(row)
                    if query:
                        if query.states and state not in query.states:
                            continue
                        if state == RuntimeStateValue.HEALTHY and not (
                            query.include_healthy or RuntimeStateValue.HEALTHY in query.states
                        ):
                            continue
                        if query.window_start is not None and not (
                            query.window_start <= event.started_at < query.window_end
                        ):
                            continue
                    placement = f"scheduled on {node or 'unknown'}"
                    reason = placement
                    if state != RuntimeStateValue.UNKNOWN or ready is not None:
                        reason = (
                            f"{placement}; health/readiness observed from telemetry"
                        )
                    payload = RuntimeStatePayload(
                        entity_id=pod,
                        runtime_kind=RuntimeKind.POD,
                        state=state,
                        reason=reason,
                        observed_at=event.started_at,
                        ready=ready,
                    )
                    evidence.append(
                        self._evidence(
                            provider=self.provider,
                            kind=EvidenceKind.RUNTIME_STATE,
                            timestamp=event.started_at,
                            summary=f"pod {pod} scheduled on {node or 'unknown'}",
                            payload=payload.model_dump(mode="json"),
                            entity_ids=[pod],
                            identity=f"{path.name}:{row_index}",
                        )
                    )
                    if len(evidence) >= limit:
                        break
        return _result(self.provider, evidence)


def _runtime_state_value(row: dict[str, str]) -> RuntimeStateValue:
    raw = next(
        (
            row.get(name)
            for name in ("STATE", "STATUS", "HEALTH", "RUNTIME_STATE")
            if row.get(name)
        ),
        None,
    )
    if not raw:
        return RuntimeStateValue.UNKNOWN
    normalized = raw.strip().casefold().replace(" ", "_")
    try:
        return RuntimeStateValue(normalized)
    except ValueError:
        return RuntimeStateValue.UNKNOWN


def _runtime_ready_value(row: dict[str, str]) -> bool | None:
    raw = next(
        (
            row.get(name)
            for name in ("READY", "READINESS", "READY_STATUS")
            if row.get(name)
        ),
        None,
    )
    if not raw:
        return None
    normalized = raw.strip().casefold()
    if normalized in {"true", "1", "yes", "ready"}:
        return True
    if normalized in {"false", "0", "no", "not_ready", "unready"}:
        return False
    return None


def build_rcaeval_providers(
    case_dir: Path,
    *,
    case_id: str,
    runtime_manifest_hash: str,
    evidence_namespace: str,
) -> list[_RcaEvalProvider]:
    common = {
        "case_id": case_id,
        "runtime_manifest_hash": runtime_manifest_hash,
        "evidence_namespace": evidence_namespace,
    }
    providers = [
        RcaEvalLogProvider(case_dir, **common),
        RcaEvalMetricProvider(case_dir, **common),
        RcaEvalTraceProvider(case_dir, **common),
        RcaEvalDependencyProvider(case_dir, **common),
        RcaEvalServiceCatalogProvider(case_dir, **common),
        RcaEvalRuntimeStateProvider(case_dir, **common),
    ]

    for provider in providers[1:]:
        provider.telemetry = providers[0].telemetry
    return providers


def incident_event_for_case(case_dir: Path, case_id: str) -> IncidentEvent:
    from backend.domain.events import IncidentSource, Severity

    started_at = datetime.now(UTC)
    telemetry = _Telemetry(case_dir)
    for path in telemetry.trace_files:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            row = next(csv.DictReader(handle), None)
        if row:
            started_at = _trace_timestamp(row, started_at) + timedelta(minutes=12)
            break
    else:
        # 没有 Trace 时也以遥测时间定位窗口，不能用执行当天的时钟过滤离线证据。
        for path in (*telemetry.log_files, *telemetry.metric_files):
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                row = next(csv.DictReader(handle), None)
            if row:
                started_at = _timestamp(row, started_at) + timedelta(minutes=12)
                break
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="unknown",
        environment="offline",
        severity=Severity.CRITICAL,
        title=f"Opaque incident {case_id}",
        description=(
            "Diagnose the affected service and failure mechanism using only the "
            "attached offline telemetry."
        ),
        started_at=started_at,
        time_window_minutes=12,
    )


def _result(
    provider: EvidenceProvider, evidence: list[EvidenceItem], *, truncated: bool = False,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.PARTIAL if truncated else ProviderStatus.SUCCESS,
        evidence_items=evidence,
        truncated=truncated, returned_count=len(evidence),
        error_message="query_result_truncated: narrow query filters" if truncated else None,
    )


def _timestamp(row: dict[str, str], fallback: datetime) -> datetime:
    for key in ("timestamp", "time"):
        value = row.get(key)
        if value:
            return _cell_timestamp(value, fallback)
    return fallback


def _cell_timestamp(value: str, fallback: datetime) -> datetime:
    text = value.strip()
    # 遥测中同时存在秒、毫秒、微秒和纳秒 Unix 时间，不能回退成事件时间。
    numeric = _finite_float(text)
    if numeric is not None:
        divisor = (
            1e9 if abs(numeric) >= 1e17 else 1e6 if abs(numeric) >= 1e14
            else 1e3 if abs(numeric) >= 1e11 else 1
        )
        try:
            return datetime.fromtimestamp(numeric / divisor, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=fallback.tzinfo or UTC)
    except ValueError:
        pass
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(text, pattern).time()
            return datetime.combine(fallback.date(), clock, fallback.tzinfo or UTC)
        except ValueError:
            continue
    return fallback


def _trace_timestamp(row: dict[str, str], fallback: datetime) -> datetime:
    value = _finite_float(row.get("startTimeMillis", ""))
    if value is not None:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return _cell_timestamp(row.get("time", ""), fallback)


def _trace_duration_ms(row: dict[str, str]) -> float:
    value = _finite_float(row.get("duration", "")) or 0.0
    # RE2 trace duration is microseconds.
    return min(3_600_000.0, max(0.0, value / 1000))


def _with_rpc_peer(span: TraceSpanPayload, trace: list[TraceSpanPayload]) -> TraceSpanPayload:
    """仅配对同 trace、同操作的跨服务父子 span，差值包含传输及未观测等待。"""
    peers = [peer for peer in trace if peer.service != span.service
             and peer.operation.lstrip("/") == span.operation.lstrip("/")
             and (peer.span_id == span.parent_span_id or peer.parent_span_id == span.span_id)]
    if len(peers) != 1:
        return span
    peer = peers[0]
    return span.model_copy(update={"attributes": {
        **span.attributes, "rpc.peer_span_id": peer.span_id, "rpc.peer_service": peer.service,
        "rpc.peer_role": "caller" if peer.span_id == span.parent_span_id else "callee",
        "rpc.peer_duration_ms": f"{peer.duration_ms:.3f}",
        "rpc.duration_difference_ms": f"{span.duration_ms - peer.duration_ms:.3f}",
    }})


def _grpc_status_code(row) -> int | None:
    """RE2 的 qualified.Service/Method 操作携带 gRPC 状态码，缺失值不推断成功。"""
    operation = (row.get("operationName") or "").lstrip("/")
    if not re.fullmatch(r"[\w.]+\.[\w]+/[\w]+", operation):
        return None
    value = _finite_float(row.get("statusCode", ""))
    return int(value) if value is not None and value.is_integer() and 0 <= value <= 16 else None


def _row_span_status(row) -> SpanStatus:
    code = _grpc_status_code(row)
    if code is not None:
        return SpanStatus.OK if code == 0 else SpanStatus.ERROR
    return _span_status(row.get("statusCode", ""))


def _span_status(value: str) -> SpanStatus:
    normalized = value.strip().casefold()
    number = _finite_float(normalized)
    if number is not None and number.is_integer():
        normalized = str(int(number))
    if normalized in {"error", "2", "500", "503"} or normalized.startswith("5"):
        return SpanStatus.ERROR
    if normalized in {"ok", "1", "200", "201", "204"}:
        return SpanStatus.OK
    return SpanStatus.UNSET


def _finite_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mean_absolute_delta(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return fmean(
        abs(right - left)
        for left, right in zip(values, values[1:], strict=False)
    )


def _entity_from_metric(name: str) -> str:
    for separator in ("_container-", "_cpu", "_memory", "_latency", "_error"):
        if separator in name:
            return name.split(separator, 1)[0][:128]
    return name.split("_", 1)[0][:128]


def _is_signal_metric(name: str) -> bool:
    """识别 RCAEval 适配器中的服务级信号列，排除原始容器噪声。"""
    return bool(_SIGNAL_METRIC_PATTERN.search(name))


def _trace_in_window(span: TraceSpanPayload, query: TraceQuery | None) -> bool:
    return query is None or query.window_start is None or (
        query.window_start <= span.started_at < query.window_end
    )


def _parse_trace_span(row: dict, fallback: datetime) -> TraceSpanPayload | None:
    try:
        return TraceSpanPayload(
            trace_id=(row.get("traceID") or "").casefold(),
            span_id=(row.get("spanID") or "").casefold(),
            parent_span_id=(row.get("parentSpanID") or "").casefold() or None,
            service=(row.get("serviceName") or "unknown").strip()[:128],
            operation=(
                row.get("operationName") or row.get("methodName") or "unknown"
            ).strip()[:128],
            started_at=_trace_timestamp(row, fallback), duration_ms=_trace_duration_ms(row),
            status=_row_span_status(row),
            attributes={
                **({"rpc.status_code": str(int(float(row["statusCode"]))),
                    "rpc.system": "grpc"} if _grpc_status_code(row) is not None else {}),
                **({"source_service": row["source_service"]} if row.get("source_service") else {}),
            },
        )
    except ValueError:
        return None


def _related_metric_names(name: str, available: list[str]) -> list[str]:
    """给汇总列附上同实体的可查询细项，不把相关指标或命名当成故障结论。"""
    if not _is_signal_metric(name):
        return []
    entity = _entity_from_metric(name)
    family = classify_metric_signal(name)
    suffixes = {
        "cpu": ("cpu-user-seconds-total", "cpu-system-seconds-total", "spec-cpu-quota"),
        "memory": ("memory-cache", "memory-rss", "memory-working-set-bytes",
                   "spec-memory-limit-bytes", "cpu-user-seconds-total",
                   "cpu-system-seconds-total"),
        "disk_io": ("fs-reads-bytes-total", "fs-writes-bytes-total",
                    "cpu-user-seconds-total", "cpu-system-seconds-total",
                    "memory-cache", "memory-rss"),
    }.get(family, ())
    peers = sorted({other for other in available
                    if other != name and _entity_from_metric(other) == entity})
    details = [other for suffix in suffixes for other in peers if other.endswith(suffix)]
    if not suffixes and family in {"socket", "latency"}:
        details = [other for other in peers
                   if "_container-" in other
                   and classify_metric_signal(other) == "network_corruption"][:4]
        if family == "socket":
            details.extend(other
                           for suffix in ("cpu-user-seconds-total", "cpu-system-seconds-total")
                           for other in peers if other.endswith(suffix))
    controls = [other for suffix in ("_workload", "_latency-90")
                for other in peers if other.endswith(suffix)]
    return list(dict.fromkeys(details + controls))[:8]



def _first_sustained_deviation(points: list[tuple[datetime, float]]) -> dict:
    """描述相对前半窗基线的持续偏离，不能当作真实故障起点或因果顺序。"""
    middle = len(points) // 2
    baseline = [value for _, value in points[:middle]]
    center = median(baseline)
    threshold = max(3 * median(abs(value - center) for value in baseline),
                    abs(center) * 0.1, 1e-9)
    for index in range(middle, len(points) - 2):
        if all(abs(value - center) > threshold for _, value in points[index:index + 3]):
            return {"timestamp": points[index][0].isoformat(), "baseline_median": center,
                    "absolute_deviation_threshold": threshold, "consecutive_samples": 3}
    return {}


def _time_profile(points: list[tuple[datetime, float]]) -> list[dict]:
    """最多八段等样本时间摘要；它只保留观测走势，不声明故障注入点或因果顺序。"""
    count = min(8, len(points))
    return [
        {"start": bucket[0][0].isoformat(), "end": bucket[-1][0].isoformat(),
         "mean": fmean(value for _, value in bucket)}
        for index in range(count)
        if (bucket := points[index * len(points) // count:(index + 1) * len(points) // count])
    ]
