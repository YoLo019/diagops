"""离线 incident package 的最小加载器与只读 Provider 适配器。

边界（spec 7.5/8.1）：
- package 是不透明目录，manifest 声明每个 source 是否故意缺席；缺席 => skipped，
  配置存在但文件缺失/损坏/全行无效 => failed，绝不回退到 mock。
- 每行原始数据先过有界 Pydantic 投影再进入 EvidenceItem.payload；原始后端
  JSON 不直接落库。部分行无效但存在有效行 => partial。
- manifest 声明的 artifact 哈希在加载时校验，不匹配按损坏处理。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvenance,
    EvidenceProvider,
    EvidenceScope,
    EvidenceSourceClass,
    RelatedAlertPayload,
    RuntimeStatePayload,
    SpanStatus,
    TraceSpanPayload,
)
from backend.domain.tool_queries import (
    DependencyDirection,
    DependencyQuery,
    DeploymentQuery,
    LogQuery,
    MetricQuery,
    RelatedAlertQuery,
    RuntimeStateQuery,
    RuntimeStateValue,
    ScopedTelemetryQuery,
    ServiceCatalogQuery,
    TraceDirection,
    TraceQuery,
)
from backend.providers.results import ProviderResult, ProviderStatus
from backend.safety.redaction import redact_text, safe_failure

PACKAGE_ADAPTER_VERSION = "local-package-v1"
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_SOURCE_ROWS = 20_000
MAX_SUMMARY_LENGTH = 240


class PackageSourceError(ValueError):
    """配置的 package source 缺失、损坏或超界。"""


class PackageTimeoutError(TimeoutError):
    """本地读取超过有界 deadline，按 timeout 分类而不是 provider_failure。"""


class PackageManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str = Field(min_length=1, max_length=128)
    source_class: EvidenceSourceClass
    sources: dict[str, PackageSourceModel] = Field(default_factory=dict)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)


class PackageSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str | None = Field(default=None, max_length=160)
    present: bool = True


@dataclass(frozen=True)
class LocalIncidentPackage:
    root: Path
    package_id: str
    source_class: EvidenceSourceClass
    sources: dict[str, PackageSourceModel]
    artifact_hashes: dict[str, str]
    adapter_version: str = PACKAGE_ADAPTER_VERSION

    @classmethod
    def load(cls, root: Path | str) -> LocalIncidentPackage:
        root_path = Path(root)
        manifest_path = root_path / "manifest.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PackageManifestModel.model_validate(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PackageSourceError("incident package manifest is missing or corrupt") from exc
        return cls(
            root=root_path,
            package_id=manifest.package_id,
            source_class=manifest.source_class,
            sources=dict(manifest.sources),
            artifact_hashes=dict(manifest.artifact_hashes),
        )

    def provenance(self, source_name: str) -> EvidenceProvenance:
        return EvidenceProvenance(
            source_class=self.source_class,
            provider_profile="local_package",
            source_artifact_id=f"{self.package_id}:{source_name}",
            source_artifact_hash=self.artifact_hashes.get(source_name),
            adapter_version=self.adapter_version,
        )


def load_incident_event(package: LocalIncidentPackage) -> IncidentEvent:
    """从 package envelope 构造 IncidentEvent；label 缺失即失败，不猜测。"""
    try:
        raw = json.loads((package.root / "incident.json").read_text(encoding="utf-8"))
        return IncidentEvent(
            source=IncidentSource(raw.get("source", "webhook")),
            service=str(raw["service"]),
            environment=str(raw["environment"]),
            severity=Severity(raw.get("severity", "critical")),
            title=str(raw.get("title", package.package_id)),
            description=str(raw.get("description", "")),
            started_at=_parse_timestamp(raw["started_at"]),
            time_window_minutes=int(raw.get("time_window_minutes", 30)),
        )
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PackageSourceError("incident envelope is missing or corrupt") from exc


@dataclass(frozen=True)
class PackageRows:
    rows: list[Any]
    malformed: int


class _PackageProviderBase:
    """统一实现 success/empty/skipped/partial/failed 状态矩阵。"""

    provider: EvidenceProvider
    supported_tools: frozenset[str]
    source_name: str

    def __init__(self, package: LocalIncidentPackage, *, row_loader=None) -> None:
        self.package = package
        # row_loader 仅用于测试/验收注入慢读取或损坏读取，生产路径为 None。
        self._row_loader = row_loader

    def _source_rows(self) -> PackageRows | ProviderResult:
        source = self.package.sources.get(self.source_name)
        if source is None or not source.present:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.SKIPPED,
                error_message=(
                    f"{self.source_name} source intentionally absent in incident manifest"
                ),
            )
        try:
            rows = self._read_rows(source)
        except PackageTimeoutError:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("timeout"),
            )
        except PackageSourceError:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        return rows

    def _read_rows(self, source: PackageSourceModel) -> PackageRows:
        if self._row_loader is not None:
            return self._row_loader(source)
        if not source.file:
            raise PackageSourceError("source declares no file")
        path = self.package.root / source.file
        try:
            # 路径必须留在 package 根内，防止 manifest 指向外部文件。
            if path.resolve().parent != self.package.root.resolve():
                raise PackageSourceError("source file escapes package root")
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise PackageSourceError("source file exceeds size limit")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except PackageSourceError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PackageSourceError("source file missing or corrupt") from exc
        declared = self.package.artifact_hashes.get(self.source_name)
        if declared is not None:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != declared:
                raise PackageSourceError("source artifact hash mismatch")
        if not isinstance(raw, (list, dict)):
            raise PackageSourceError("source file must contain a JSON array or object")
        return self._coerce_rows(raw)

    def _coerce_rows(self, raw) -> PackageRows:
        if not isinstance(raw, list):
            raise PackageSourceError("source file must contain a JSON array")
        return PackageRows(rows=raw[:MAX_SOURCE_ROWS], malformed=0)

    def _collect(
        self,
        event: IncidentEvent,
        query,
        row_kind: type[BaseModel],
        emit,
    ) -> ProviderResult:
        loaded = self._source_rows()
        if isinstance(loaded, ProviderResult):
            return loaded
        valid: list[EvidenceItem] = []
        malformed = loaded.malformed
        started = datetime.now(UTC)
        for row in loaded.rows:
            if len(valid) >= (query.limit if query is not None else 50):
                break
            try:
                parsed = row_kind.model_validate(row)
            except (TypeError, ValueError):
                malformed += 1
                continue
            item = emit(self, parsed, event, query)
            if item is not None:
                valid.append(item)
            if (datetime.now(UTC) - started).total_seconds() > 25:
                return ProviderResult(
                    provider=self.provider,
                    status=ProviderStatus.FAILED,
                    error_message=safe_failure("timeout"),
                )
        if malformed and not valid:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        if malformed:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.PARTIAL,
                evidence_items=valid,
                error_message=f"{malformed} malformed {self.source_name} rows discarded",
            )
        return ProviderResult(provider=self.provider, evidence_items=valid)


# --- 行级投影模型（有界字段，先归一化再存储） ---


class _LogRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    timestamp: datetime
    level: str = Field(min_length=1, max_length=32)
    message: str = Field(min_length=1, max_length=1024)
    service: str | None = Field(default=None, max_length=128)
    entity_id: str | None = Field(default=None, max_length=128)


class _MetricPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: datetime
    value: float = Field(allow_inf_nan=False)


class _MetricRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    entity_id: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=32)
    points: list[_MetricPoint] = Field(default_factory=list, max_length=10_000)


class _CatalogServiceRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    owner: str | None = Field(default=None, max_length=128)
    dependencies: list[str] = Field(default_factory=list, max_length=20)


class _CatalogRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    services: list[_CatalogServiceRow] = Field(default_factory=list, max_length=200)


class _DeploymentRow(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    service: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=120)
    deployed_at: datetime
    environment: str | None = Field(default=None, max_length=128)


class PackageLogProvider(_PackageProviderBase):
    provider = EvidenceProvider.LOG
    supported_tools = frozenset({"read_logs"})
    source_name = "logs"

    def collect(self, event: IncidentEvent, query: LogQuery | None = None) -> ProviderResult:
        return self._collect(event, query, _LogRow, _emit_log)


class PackageMetricProvider(_PackageProviderBase):
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})
    source_name = "metrics"

    def collect(self, event: IncidentEvent, query: MetricQuery | None = None) -> ProviderResult:
        return self._collect(event, query, _MetricRow, _emit_metric)


class FileTraceProvider(_PackageProviderBase):
    """默认的本地 trace Provider；与 TempoTraceProvider 共用 canonical span 投影。"""

    provider = EvidenceProvider.TRACE
    supported_tools = frozenset({"query_traces"})
    source_name = "spans"

    def collect(self, event: IncidentEvent, query: TraceQuery | None = None) -> ProviderResult:
        loaded = self._source_rows()
        if isinstance(loaded, ProviderResult):
            return loaded
        spans: list[TraceSpanPayload] = []
        malformed = loaded.malformed
        for row in loaded.rows:
            try:
                spans.append(TraceSpanPayload.model_validate(row))
            except (TypeError, ValueError):
                malformed += 1
        if malformed and not spans:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        from backend.providers.trace_timing import with_child_timing

        selected = select_trace_spans(with_child_timing(spans), event, query)
        latency_highlight = query is not None and query.min_duration_ms is not None
        evidence = [
            _span_to_evidence(self, span, latency_highlight=latency_highlight)
            for span in selected
        ]
        status = ProviderStatus.PARTIAL if malformed else ProviderStatus.SUCCESS
        return ProviderResult(
            provider=self.provider,
            status=status,
            evidence_items=evidence,
            error_message=(
                f"{malformed} malformed spans rows discarded" if malformed else None
            ),
        )


def select_trace_spans(
    spans: list[TraceSpanPayload],
    event: IncidentEvent,
    query: TraceQuery | None,
) -> list[TraceSpanPayload]:
    """应用 trace 过滤与 direction 扩展；File 与 Tempo 路径共用同一语义。"""
    start, end = _scoped_window(event, query)
    limit = query.limit if query is not None else 50

    def is_anchor(span: TraceSpanPayload) -> bool:
        if query is None:
            return start <= span.started_at <= end
        if query.trace_id is not None and span.trace_id != query.trace_id:
            return False
        if query.service is not None and span.service != query.service:
            return False
        if query.operation is not None and span.operation != query.operation:
            return False
        if query.error_only and span.status != SpanStatus.ERROR:
            return False
        if query.min_duration_ms is not None and span.duration_ms < query.min_duration_ms:
            return False
        if query.entity_ids and span.service not in query.entity_ids:
            return False
        return start <= span.started_at <= end

    anchors = [span for span in spans if is_anchor(span)]
    if query is None:
        return sorted(spans, key=lambda span: (span.started_at, span.span_id))[:limit]
    direction = query.direction
    # 无 service/operation 锚定条件时方向扩展没有参照点，直接返回匹配集合。
    if query.service is None and query.operation is None:
        direction = TraceDirection.BOTH
        return sorted(anchors, key=lambda span: (span.started_at, span.span_id))[:limit]
    return expand_span_selection(spans, anchors, direction)[:limit]


class PackageServiceCatalogProvider(_PackageProviderBase):
    provider = EvidenceProvider.SERVICE_CATALOG
    supported_tools = frozenset({"read_service_catalog"})
    source_name = "catalog"

    def _coerce_rows(self, raw) -> PackageRows:
        # catalog.json 允许单对象包裹 services 数组，归一化为逐行服务。
        if isinstance(raw, dict) and "services" in raw:
            return PackageRows(rows=list(raw.get("services", []))[:MAX_SOURCE_ROWS], malformed=0)
        return super()._coerce_rows(raw)

    def collect(
        self, event: IncidentEvent, query: ServiceCatalogQuery | None = None
    ) -> ProviderResult:
        return self._collect(event, query, _CatalogServiceRow, _emit_catalog_service)


class PackageDeploymentProvider(_PackageProviderBase):
    provider = EvidenceProvider.DEPLOY
    supported_tools = frozenset({"read_deployments"})
    source_name = "deployments"

    def collect(
        self, event: IncidentEvent, query: DeploymentQuery | None = None
    ) -> ProviderResult:
        return self._collect(event, query, _DeploymentRow, _emit_deployment)


class PackageRuntimeStateProvider(_PackageProviderBase):
    provider = EvidenceProvider.RUNTIME_STATE
    supported_tools = frozenset({"read_runtime_state"})
    source_name = "runtime_state"

    def collect(
        self, event: IncidentEvent, query: RuntimeStateQuery | None = None
    ) -> ProviderResult:
        return self._collect(event, query, RuntimeStatePayload, _emit_runtime_state)


class PackageRelatedAlertProvider(_PackageProviderBase):
    provider = EvidenceProvider.RELATED_ALERT
    supported_tools = frozenset({"query_related_alerts"})
    source_name = "related_alerts"

    def collect(
        self, event: IncidentEvent, query: RelatedAlertQuery | None = None
    ) -> ProviderResult:
        return self._collect(event, query, RelatedAlertPayload, _emit_related_alert)


class PackageDependencyProvider(_PackageProviderBase):
    """动态边只来自 parent/child span；catalog 静态边只做可选增补。"""

    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})
    source_name = "spans"

    def collect(
        self, event: IncidentEvent, query: DependencyQuery | None = None
    ) -> ProviderResult:
        loaded = self._source_rows()
        if isinstance(loaded, ProviderResult):
            return loaded
        spans: list[TraceSpanPayload] = []
        malformed = loaded.malformed
        for row in loaded.rows:
            try:
                spans.append(TraceSpanPayload.model_validate(row))
            except (TypeError, ValueError):
                malformed += 1
        edges = derive_span_edges(spans)
        edges |= self._catalog_edges()
        target = (query.target if query else None) or event.service
        direction = query.direction if query else DependencyDirection.DOWNSTREAM
        selected = [
            edge
            for edge in sorted(edges, key=lambda item: (item[0], item[1], item[2]))
            if (direction == DependencyDirection.DOWNSTREAM and edge[0] == target)
            or (direction == DependencyDirection.UPSTREAM and edge[1] == target)
        ][: (query.limit if query else 50)]
        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.DEPENDENCY_HEALTH,
                timestamp=event.started_at,
                summary=f"{target} {direction.value} dependency edges: {len(selected)}",
                payload={
                    "target": target,
                    "direction": direction.value,
                    "edges": [
                        {"parent": parent, "child": child, "source": origin}
                        for parent, child, origin in selected
                    ],
                },
                confidence=1.0,
                scope=EvidenceScope(
                    entity_ids=[target],
                    observed_at=event.started_at,
                    signal_type="dependency",
                ),
                provenance=self.package.provenance(self.source_name),
            )
        ] if selected else []
        if malformed and not spans:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        status = ProviderStatus.PARTIAL if malformed else ProviderStatus.SUCCESS
        return ProviderResult(
            provider=self.provider,
            status=status,
            evidence_items=evidence,
            error_message=(
                f"{malformed} malformed spans rows discarded" if malformed else None
            ),
        )

    def _catalog_edges(self) -> set[tuple[str, str, str]]:
        source = self.package.sources.get("catalog")
        if source is None or not source.present or not source.file:
            return set()
        try:
            raw = json.loads((self.package.root / source.file).read_text(encoding="utf-8"))
            catalog = _CatalogRow.model_validate(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # 静态增补失败不拖垮动态边；动态推导仍是主事实来源。
            return set()
        return {
            (service.name, dependency, "catalog")
            for service in catalog.services
            for dependency in service.dependencies
            if dependency != service.name
        }


def build_package_providers(package: LocalIncidentPackage) -> list:
    """同一 package 的全部只读 Provider；registry 与离线验收共用此一构造。"""
    return [
        PackageLogProvider(package),
        PackageMetricProvider(package),
        FileTraceProvider(package),
        PackageServiceCatalogProvider(package),
        PackageDeploymentProvider(package),
        PackageRuntimeStateProvider(package),
        PackageRelatedAlertProvider(package),
        PackageDependencyProvider(package),
    ]


def derive_span_edges(spans: list[TraceSpanPayload]) -> set[tuple[str, str, str]]:
    """仅由同 trace 的 parent/child 关系推导跨服务边，不按名称共现猜测。"""
    by_id = {span.span_id: span for span in spans}
    edges: set[tuple[str, str, str]] = set()
    for span in spans:
        if span.parent_span_id is None:
            continue
        parent = by_id.get(span.parent_span_id)
        if parent is None or parent.service == span.service:
            continue
        edges.add((parent.service, span.service, "spans"))
    return edges


def expand_span_selection(
    spans: list[TraceSpanPayload],
    anchors: list[TraceSpanPayload],
    direction: TraceDirection,
) -> list[TraceSpanPayload]:
    """direction 只围绕锚点沿 parent/child 链扩展，保持方向语义可解释。"""
    by_id = {span.span_id: span for span in spans}
    selected = {span.span_id: span for span in anchors}
    if direction in (TraceDirection.UPSTREAM, TraceDirection.BOTH):
        frontier = list(anchors)
        while frontier:
            current = frontier.pop()
            parent = (
                by_id.get(current.parent_span_id)
                if current.parent_span_id is not None
                else None
            )
            if parent is not None and parent.span_id not in selected:
                selected[parent.span_id] = parent
                frontier.append(parent)
    if direction in (TraceDirection.DOWNSTREAM, TraceDirection.BOTH):
        children: dict[str, list[TraceSpanPayload]] = {}
        for span in spans:
            if span.parent_span_id is not None:
                children.setdefault(span.parent_span_id, []).append(span)
        frontier = list(anchors)
        while frontier:
            current = frontier.pop()
            for child in children.get(current.span_id, []):
                if child.span_id not in selected:
                    selected[child.span_id] = child
                    frontier.append(child)
    return sorted(selected.values(), key=lambda span: (span.started_at, span.span_id))


# --- 行 -> evidence 投影 ---


def _event_window(event: IncidentEvent) -> tuple[datetime, datetime]:
    window = timedelta(minutes=event.time_window_minutes)
    return event.started_at - window, event.started_at + window


def _scoped_window(
    event: IncidentEvent, query: ScopedTelemetryQuery | None
) -> tuple[datetime, datetime]:
    if query is not None and query.window_start is not None and query.window_end is not None:
        return query.window_start, query.window_end
    return _event_window(event)


def _entity_match(entity: str | None, query: ScopedTelemetryQuery | None) -> bool:
    if query is None or not query.entity_ids:
        return True
    return entity is not None and entity in query.entity_ids


def _emit_log(
    self: PackageLogProvider, row: _LogRow, event: IncidentEvent, query: LogQuery | None
) -> EvidenceItem | None:
    if row.service is not None and row.service != event.service:
        return None
    if query is not None:
        if not query.start_time <= row.timestamp <= query.end_time:
            return None
        if query.keywords and not all(
            keyword.lower() in row.message.lower() for keyword in query.keywords
        ):
            return None
        if query.levels and not any(
            row.level.lower() == level.lower() for level in query.levels
        ):
            return None
        if query.instance and row.entity_id != query.instance:
            return None
    else:
        start, end = _event_window(event)
        if not start <= row.timestamp <= end:
            return None
    message = redact_text(row.message)[:MAX_SUMMARY_LENGTH]
    payload: dict[str, Any] = {
        "level": row.level,
        "message": message,
        "service": row.service or event.service,
    }
    if row.entity_id:
        payload["entity_id"] = row.entity_id
    return EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=row.timestamp,
        summary=f"{row.level}: {message[:160]}",
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[row.entity_id] if row.entity_id else [event.service],
            observed_at=row.timestamp,
            signal_type="log",
        ),
        provenance=self.package.provenance(self.source_name),
    )


def _emit_metric(
    self: PackageMetricProvider, row: _MetricRow, event: IncidentEvent, query: MetricQuery | None
) -> EvidenceItem | None:
    if query is not None:
        if query.metric_names and row.name not in query.metric_names:
            return None
        if query.instance and row.entity_id != query.instance:
            return None
        window = (query.start_time, query.end_time)
    else:
        window = _event_window(event)
    points = [point for point in row.points if window[0] <= point.timestamp <= window[1]]
    if not points:
        return None
    values = [point.value for point in points]
    aggregation = query.aggregation.value if query is not None else "avg"
    if aggregation == "max":
        aggregate = max(values)
    elif aggregation == "sum":
        aggregate = sum(values)
    else:
        aggregate = sum(values) / len(values)
    payload: dict[str, Any] = {
        "metric_name": row.name,
        "aggregation": aggregation,
        "value": round(aggregate, 6),
        "sample_count": len(values),
    }
    if row.entity_id:
        payload["entity_id"] = row.entity_id
    if row.unit:
        payload["unit"] = row.unit
    return EvidenceItem(
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=points[0].timestamp,
        summary=(
            f"{row.name} {aggregation}={payload['value']} over {len(values)} samples"
        ),
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[row.entity_id] if row.entity_id else [],
            window_start=window[0],
            window_end=window[1],
            signal_type="metric",
        ),
        provenance=self.package.provenance(self.source_name),
    )


def _span_to_evidence(
    provider: FileTraceProvider,
    span: TraceSpanPayload,
    *,
    latency_highlight: bool = False,
) -> EvidenceItem:
    if span.status == SpanStatus.ERROR:
        kind = EvidenceKind.TRACE_ERROR
    elif latency_highlight:
        kind = EvidenceKind.TRACE_LATENCY
    else:
        kind = EvidenceKind.TRACE_PATH
    payload = span.model_dump(mode="json")
    return EvidenceItem(
        provider=EvidenceProvider.TRACE,
        kind=kind,
        timestamp=span.started_at,
        summary=(
            f"{span.service}/{span.operation} {span.status.value} "
            f"{span.duration_ms}ms trace={span.trace_id}"
        )[:MAX_SUMMARY_LENGTH],
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[span.service],
            observed_at=span.started_at,
            signal_type="trace",
        ),
        provenance=provider.package.provenance(provider.source_name),
    )


def _emit_catalog_service(
    self: PackageServiceCatalogProvider,
    row: _CatalogServiceRow,
    event: IncidentEvent,
    query: ServiceCatalogQuery | None,
) -> EvidenceItem | None:
    if query is not None and query.name is not None and row.name != query.name:
        return None
    include_dependencies = query.include_dependencies if query is not None else True
    payload: dict[str, Any] = {"name": row.name}
    if row.owner:
        payload["owner"] = row.owner
    if include_dependencies:
        payload["dependencies"] = list(row.dependencies)
    return EvidenceItem(
        provider=EvidenceProvider.SERVICE_CATALOG,
        kind=EvidenceKind.SERVICE_METADATA,
        timestamp=event.started_at,
        summary=f"service catalog entry {row.name}",
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(entity_ids=[row.name], signal_type="service_catalog"),
        provenance=self.package.provenance(self.source_name),
    )


def _emit_deployment(
    self: PackageDeploymentProvider,
    row: _DeploymentRow,
    event: IncidentEvent,
    query: DeploymentQuery | None,
) -> EvidenceItem | None:
    if row.environment is not None and row.environment != event.environment:
        return None
    if query is not None:
        if not query.start_time <= row.deployed_at <= query.end_time:
            return None
        if query.version and row.version != query.version:
            return None
        if query.instance and row.service != query.instance:
            return None
    else:
        start, end = _event_window(event)
        if not start <= row.deployed_at <= end:
            return None
    return EvidenceItem(
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=row.deployed_at,
        summary=f"{row.service} deployed {row.version}",
        payload={
            "service": row.service,
            "version": row.version,
            "deployed_at": row.deployed_at.isoformat(),
        },
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[row.service],
            observed_at=row.deployed_at,
            signal_type="deployment",
        ),
        provenance=self.package.provenance(self.source_name),
    )


def _emit_runtime_state(
    self: PackageRuntimeStateProvider,
    row: RuntimeStatePayload,
    event: IncidentEvent,
    query: RuntimeStateQuery | None,
) -> EvidenceItem | None:
    start, end = _scoped_window(event, query)
    if not start <= row.observed_at <= end:
        return None
    if query is not None:
        if not _entity_match(row.entity_id, query):
            return None
        if query.states and row.state not in query.states:
            return None
        if row.state == RuntimeStateValue.HEALTHY and not (
            query.include_healthy or RuntimeStateValue.HEALTHY in query.states
        ):
            return None
    elif row.state == RuntimeStateValue.HEALTHY:
        return None
    payload = row.model_dump(mode="json")
    return EvidenceItem(
        provider=EvidenceProvider.RUNTIME_STATE,
        kind=EvidenceKind.RUNTIME_STATE,
        timestamp=row.observed_at,
        summary=f"{row.entity_id} {row.state.value}: {row.reason[:160]}",
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[row.entity_id],
            observed_at=row.observed_at,
            signal_type="runtime_state",
        ),
        provenance=self.package.provenance(self.source_name),
    )


def _emit_related_alert(
    self: PackageRelatedAlertProvider,
    row: RelatedAlertPayload,
    event: IncidentEvent,
    query: RelatedAlertQuery | None,
) -> EvidenceItem | None:
    start, end = _scoped_window(event, query)
    alert_end = row.ends_at or row.starts_at
    if alert_end < start or row.starts_at > end:
        return None
    if query is not None:
        if not _entity_match(row.entity_id, query):
            return None
        if query.severities and row.severity not in query.severities:
            return None
        if query.statuses and row.status not in query.statuses:
            return None
    payload = row.model_dump(mode="json")
    return EvidenceItem(
        provider=EvidenceProvider.RELATED_ALERT,
        kind=EvidenceKind.RELATED_ALERT,
        timestamp=row.starts_at,
        summary=f"{row.severity.value} alert {row.name} on {row.entity_id}",
        payload=payload,
        confidence=1.0,
        scope=EvidenceScope(
            entity_ids=[row.entity_id],
            observed_at=row.starts_at,
            signal_type="related_alert",
        ),
        provenance=self.package.provenance(self.source_name),
    )


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
