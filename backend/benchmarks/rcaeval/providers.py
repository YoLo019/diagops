"""RCAEval 匿名遥测包的只读 Provider 适配器。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

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
from backend.providers.results import ProviderResult, ProviderStatus

ADAPTER_VERSION = "rcaeval-re2-v1"
_MAX_SCAN_ROWS = 250_000


class _Telemetry:
    def __init__(self, case_dir: Path) -> None:
        self.case_dir = case_dir.resolve()
        self.files = tuple(sorted(self.case_dir.glob("telemetry-*")))
        self._headers: dict[Path, list[str]] = {}
        for path in self.files:
            if path.suffix.casefold() != ".csv":
                continue
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                self._headers[path] = next(csv.reader(handle), [])

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
        canonical = json.dumps(
            [
                self.case_id,
                provider.value,
                kind.value,
                summary,
                payload,
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
            summary=summary[:512],
            payload=payload,
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
        for path in self.telemetry.log_files:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for row_index, row in enumerate(csv.DictReader(handle)):
                    if row_index >= _MAX_SCAN_ROWS or len(evidence) >= limit:
                        break
                    service = (row.get("container_name") or "unknown").strip()
                    message = (row.get("message") or "").strip()
                    level = (row.get("level") or "").strip()
                    searchable = f"{service} {message}".casefold()
                    if not message or (
                        keywords and not all(item in searchable for item in keywords)
                    ):
                        continue
                    if levels and level.casefold() not in levels:
                        continue
                    if instance and instance not in service.casefold():
                        continue
                    timestamp = _timestamp(row, event.started_at)
                    evidence.append(
                        self._evidence(
                            provider=self.provider,
                            kind=EvidenceKind.LOG_PATTERN,
                            timestamp=timestamp,
                            summary=f"log signal on {service}: {message[:240]}",
                            payload={
                                "service": service[:128],
                                "level": level[:32],
                                "message": message[:500],
                            },
                            entity_ids=[service[:128]],
                        )
                    )
        return _result(self.provider, evidence)


class RcaEvalMetricProvider(_RcaEvalProvider):
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})

    def collect(
        self, event: IncidentEvent, query: MetricQuery | None = None
    ) -> ProviderResult:
        requested = [item.casefold() for item in (query.metric_names if query else [])]
        instance = query.instance.casefold() if query and query.instance else None
        limit = query.limit if query else 20
        changes: list[tuple[float, str, str, float, float, datetime]] = []
        for path in self.telemetry.metric_files:
            header = self.telemetry._headers[path]
            selected = [
                (index, name)
                for index, name in enumerate(header[1:], start=1)
                if (not requested or any(token in name.casefold() for token in requested))
                and (not instance or instance in name.casefold())
            ]
            if not selected:
                continue
            # 无过滤时限制列数；Agent 可通过 metric_names/instance 精确展开。
            selected = selected[: 400 if requested or instance else 120]
            series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                for row_index, row in enumerate(reader):
                    if row_index >= _MAX_SCAN_ROWS:
                        break
                    timestamp = _cell_timestamp(row[0] if row else "", event.started_at)
                    for index, name in selected:
                        if index >= len(row):
                            continue
                        value = _finite_float(row[index])
                        if value is not None:
                            series[name].append((timestamp, value))
            for name, points in series.items():
                if len(points) < 4:
                    continue
                middle = len(points) // 2
                before = [value for _, value in points[:middle]]
                after = [value for _, value in points[middle:]]
                baseline = fmean(before)
                observation = fmean(after)
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
                    )
                )
        evidence = [
            self._evidence(
                provider=self.provider,
                kind=EvidenceKind.METRIC_TREND,
                timestamp=timestamp,
                summary=(
                    f"{metric[:180]} changed on {entity}: baseline={baseline:.6g}, "
                    f"observation={observation:.6g}, change_score={score:.3f}"
                ),
                payload={
                    "entity": entity,
                    "metric": metric[:256],
                    "baseline_mean": baseline,
                    "observation_mean": observation,
                    "change_score": score,
                },
                entity_ids=[entity],
            )
            for score, metric, entity, baseline, observation, timestamp in sorted(
                changes, reverse=True
            )[:limit]
        ]
        return _result(self.provider, evidence)


class RcaEvalTraceProvider(_RcaEvalProvider):
    provider = EvidenceProvider.TRACE
    supported_tools = frozenset({"query_traces"})

    def collect(self, event: IncidentEvent, query: TraceQuery | None = None) -> ProviderResult:
        limit = query.limit if query else 50
        evidence: list[EvidenceItem] = []
        for row in self._rows():
            service = (row.get("serviceName") or "unknown").strip()
            operation = (row.get("operationName") or row.get("methodName") or "unknown").strip()
            duration = _trace_duration_ms(row)
            status = _span_status(row.get("statusCode", ""))
            if query:
                if query.service and query.service != service:
                    continue
                if query.operation and query.operation not in operation:
                    continue
                if query.trace_id and query.trace_id != row.get("traceID"):
                    continue
                if query.error_only and status != SpanStatus.ERROR:
                    continue
                if query.min_duration_ms is not None and duration < query.min_duration_ms:
                    continue
            timestamp = _trace_timestamp(row, event.started_at)
            try:
                span = TraceSpanPayload(
                    trace_id=(row.get("traceID") or "").casefold(),
                    span_id=(row.get("spanID") or "").casefold(),
                    parent_span_id=(row.get("parentSpanID") or None),
                    service=service[:128],
                    operation=operation[:128],
                    started_at=timestamp,
                    duration_ms=duration,
                    status=status,
                )
            except ValueError:
                continue
            evidence.append(
                self._evidence(
                    provider=self.provider,
                    kind=(
                        EvidenceKind.TRACE_ERROR
                        if status == SpanStatus.ERROR
                        else EvidenceKind.TRACE_LATENCY
                    ),
                    timestamp=timestamp,
                    summary=(
                        f"trace {status.value} on {service}: "
                        f"{operation[:160]} {duration:.3f}ms"
                    ),
                    payload=span.model_dump(mode="json"),
                    entity_ids=[service[:128]],
                )
            )
            if len(evidence) >= limit:
                break
        return _result(self.provider, evidence)

    def _rows(self):
        for path in self.telemetry.trace_files:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for index, row in enumerate(csv.DictReader(handle)):
                    if index >= _MAX_SCAN_ROWS:
                        return
                    yield row


class RcaEvalDependencyProvider(RcaEvalTraceProvider):
    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})

    def collect(
        self, event: IncidentEvent, query: DependencyQuery | None = None
    ) -> ProviderResult:
        limit = query.limit if query else 20
        spans: dict[tuple[str, str], tuple[str, str | None, datetime, float, bool]] = {}
        for row in self._rows():
            trace_id = (row.get("traceID") or "").casefold()
            span_id = (row.get("spanID") or "").casefold()
            service = (row.get("serviceName") or "unknown")[:128]
            if trace_id and span_id:
                spans[(trace_id, span_id)] = (
                    service,
                    (row.get("parentSpanID") or None),
                    _trace_timestamp(row, event.started_at),
                    _trace_duration_ms(row),
                    _span_status(row.get("statusCode", "")) != SpanStatus.ERROR,
                )
        edges: dict[tuple[str, str], list[tuple[datetime, float, bool]]] = defaultdict(list)
        for (trace_id, _), (child, parent_id, timestamp, duration, success) in spans.items():
            parent = spans.get((trace_id, parent_id or ""))
            if parent is None or parent[0] == child:
                continue
            source, target = parent[0], child
            if query and query.target and query.target not in {source, target}:
                continue
            edges[(source, target)].append((timestamp, duration, success))
        evidence = []
        ranked_edges = sorted(
            edges.items(),
            key=lambda item: (sum(not row[2] for row in item[1]), len(item[1])),
            reverse=True,
        )
        for (source, target), samples in ranked_edges[:limit]:
            errors = sum(not row[2] for row in samples)
            timestamp = max(row[0] for row in samples)
            latency = fmean(row[1] for row in samples)
            evidence.append(
                self._evidence(
                    provider=self.provider,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=timestamp,
                    summary=(
                        f"dependency {source} -> {target}: calls={len(samples)}, "
                        f"errors={errors}, mean_duration_ms={latency:.3f}"
                    ),
                    payload={
                        "source": source,
                        "target": target,
                        "calls": len(samples),
                        "errors": errors,
                        "mean_duration_ms": latency,
                    },
                    entity_ids=[source, target],
                )
            )
        return _result(self.provider, evidence)


class RcaEvalServiceCatalogProvider(RcaEvalTraceProvider):
    provider = EvidenceProvider.SERVICE_CATALOG
    supported_tools = frozenset({"read_service_catalog"})

    def collect(
        self, event: IncidentEvent, query: ServiceCatalogQuery | None = None
    ) -> ProviderResult:
        services: set[str] = set()
        for row in self._rows():
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
        return _result(self.provider, evidence)


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
    return [
        RcaEvalLogProvider(case_dir, **common),
        RcaEvalMetricProvider(case_dir, **common),
        RcaEvalTraceProvider(case_dir, **common),
        RcaEvalDependencyProvider(case_dir, **common),
        RcaEvalServiceCatalogProvider(case_dir, **common),
        RcaEvalRuntimeStateProvider(case_dir, **common),
    ]


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


def _result(provider: EvidenceProvider, evidence: list[EvidenceItem]) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.SUCCESS,
        evidence_items=evidence,
    )


def _timestamp(row: dict[str, str], fallback: datetime) -> datetime:
    for key in ("timestamp", "time"):
        value = row.get(key)
        if value:
            return _cell_timestamp(value, fallback)
    return fallback


def _cell_timestamp(value: str, fallback: datetime) -> datetime:
    text = value.strip()
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


def _span_status(value: str) -> SpanStatus:
    normalized = value.strip().casefold()
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
