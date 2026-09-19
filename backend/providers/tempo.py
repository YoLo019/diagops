"""Tempo trace Provider：与 FileTraceProvider 共用 query_traces 契约与 canonical 投影。

边界（spec 7.7/R23）：
- Agent 查询与归一化 Evidence 永远使用 canonical 事件时间与 canonical trace/span
  ID；本 Provider 负责把 canonical 查询界映射到 replay 时偏移后的 backend 值，
  并在 scope 校验与 evidence 生成前映射回来。
- File 是强制默认 profile；Tempo 是唯一声称的真实 trace backend，且必须先通过
  Docker-local parity gate 才能声称支持。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvenance,
    EvidenceProvider,
    EvidenceScope,
    EvidenceSourceClass,
    SpanStatus,
    TraceSpanPayload,
)
from backend.domain.tool_queries import TraceQuery
from backend.providers.local_package import (
    MAX_SUMMARY_LENGTH,
    select_trace_spans,
)
from backend.providers.results import ProviderResult, ProviderStatus
from backend.safety.redaction import safe_failure

TEMPO_ADAPTER_VERSION = "tempo-trace-v1"
MAX_TRACES_PER_QUERY = 20

_STATUS_BY_CODE = {
    0: SpanStatus.UNSET,
    1: SpanStatus.OK,
    2: SpanStatus.ERROR,
    "STATUS_CODE_UNSET": SpanStatus.UNSET,
    "STATUS_CODE_OK": SpanStatus.OK,
    "STATUS_CODE_ERROR": SpanStatus.ERROR,
}


@dataclass(frozen=True)
class ReplayMapping:
    """canonical <-> backend 的可逆时间/ID 映射；offset 对全部 span 恒定。"""

    offset_ms: int = 0
    trace_id_map: dict[str, str] = field(default_factory=dict)
    span_id_map: dict[str, str] = field(default_factory=dict)

    def to_backend_trace_id(self, trace_id: str) -> str:
        return self.trace_id_map.get(trace_id, trace_id)

    def to_backend_time(self, value: datetime) -> datetime:
        return value + timedelta(milliseconds=self.offset_ms)

    def to_canonical_trace_id(self, trace_id: str) -> str:
        inverse = {backend: canonical for canonical, backend in self.trace_id_map.items()}
        return inverse.get(trace_id, trace_id)

    def to_canonical_span_id(self, span_id: str) -> str:
        inverse = {backend: canonical for canonical, backend in self.span_id_map.items()}
        return inverse.get(span_id, span_id)

    def to_canonical_time(self, value: datetime) -> datetime:
        return value - timedelta(milliseconds=self.offset_ms)


class TempoTraceProvider:
    """通过 Tempo HTTP API 提供 query_traces；查询界与结果都做双向映射。"""

    provider = EvidenceProvider.TRACE
    supported_tools = frozenset({"query_traces"})

    def __init__(
        self,
        base_url: str,
        *,
        mapping: ReplayMapping | None = None,
        provenance: EvidenceProvenance | None = None,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.mapping = mapping or ReplayMapping()
        self.provenance = provenance or EvidenceProvenance(
            source_class=EvidenceSourceClass.PUBLIC_DATASET,
            provider_profile="tempo_local",
            adapter_version=TEMPO_ADAPTER_VERSION,
        )
        self.timeout_seconds = timeout_seconds
        # client 仅测试注入；生产路径按需创建并显式关闭。
        self._client = client

    def collect(self, event: IncidentEvent, query: TraceQuery | None = None) -> ProviderResult:
        window = _canonical_window(event, query)
        backend_start = self.mapping.to_backend_time(window[0])
        backend_end = self.mapping.to_backend_time(window[1])
        try:
            raw_traces = self._fetch_traces(backend_start, backend_end, query)
        except httpx.TimeoutException:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("timeout"),
            )
        except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        spans: list[TraceSpanPayload] = []
        malformed = 0
        for trace in raw_traces:
            for span in _project_otlp_trace(trace, self.mapping):
                if span is None:
                    malformed += 1
                else:
                    spans.append(span)
        if malformed and not spans:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.FAILED,
                error_message=safe_failure("provider_failure"),
            )
        from backend.providers.trace_timing import with_child_timing

        selected = select_trace_spans(with_child_timing(spans), event, query)
        evidence = [self._to_evidence(span) for span in selected]
        status = ProviderStatus.PARTIAL if malformed else ProviderStatus.SUCCESS
        return ProviderResult(
            provider=self.provider,
            status=status,
            evidence_items=evidence,
            error_message=(
                f"{malformed} malformed tempo spans discarded" if malformed else None
            ),
        )

    def _fetch_traces(
        self,
        backend_start: datetime,
        backend_end: datetime,
        query: TraceQuery | None,
    ) -> list[dict[str, Any]]:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        try:
            if query is not None and query.trace_id is not None:
                backend_id = self.mapping.to_backend_trace_id(query.trace_id)
                return [
                    self._get_json(
                        client,
                        f"/api/traces/{backend_id}",
                        params=_unix_params(backend_start, backend_end),
                    )
                ]
            params: dict[str, Any] = {
                **_unix_params(backend_start, backend_end),
                "limit": MAX_TRACES_PER_QUERY,
            }
            if query is not None and query.service is not None:
                params["q"] = f'{{ resource.service.name = "{query.service}" }}'
            search = self._get_json(client, "/api/search", params=params)
            traces = []
            for entry in search.get("traces", [])[:MAX_TRACES_PER_QUERY]:
                trace_id = entry.get("traceID")
                if not trace_id:
                    continue
                traces.append(
                    self._get_json(
                        client,
                        f"/api/traces/{trace_id}",
                        params=_unix_params(backend_start, backend_end),
                    )
                )
            return traces
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _get_json(client: httpx.Client, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("tempo response must be a JSON object")
        return payload

    def _to_evidence(self, span: TraceSpanPayload) -> EvidenceItem:
        if span.status == SpanStatus.ERROR:
            kind = EvidenceKind.TRACE_ERROR
        else:
            kind = EvidenceKind.TRACE_PATH
        return EvidenceItem(
            provider=self.provider,
            kind=kind,
            timestamp=span.started_at,
            summary=(
                f"{span.service}/{span.operation} {span.status.value} "
                f"{span.duration_ms}ms trace={span.trace_id}"
            )[:MAX_SUMMARY_LENGTH],
            payload=span.model_dump(mode="json"),
            confidence=1.0,
            scope=EvidenceScope(
                entity_ids=[span.service],
                observed_at=span.started_at,
                signal_type="trace",
            ),
            provenance=self.provenance,
        )


def _canonical_window(
    event: IncidentEvent, query: TraceQuery | None
) -> tuple[datetime, datetime]:
    if query is not None and query.window_start is not None and query.window_end is not None:
        return query.window_start, query.window_end
    window = timedelta(minutes=event.time_window_minutes)
    return event.started_at - window, event.started_at + window


def _unix_params(start: datetime, end: datetime) -> dict[str, int]:
    return {"start": int(start.timestamp()), "end": int(end.timestamp())}


def _project_otlp_trace(
    trace: dict[str, Any], mapping: ReplayMapping
) -> list[TraceSpanPayload | None]:
    """把 OTLP JSON batch 投影为 canonical span；无效 span 返回 None 计为 malformed。"""
    projected: list[TraceSpanPayload | None] = []
    for batch in trace.get("batches", []):
        resource_attrs = _attributes(batch.get("resource", {}).get("attributes", []))
        service = resource_attrs.get("service.name", "unknown")
        for scope_spans in batch.get("scopeSpans", []):
            for raw in scope_spans.get("spans", []):
                projected.append(_project_span(raw, service, mapping))
    return projected


def _project_span(
    raw: dict[str, Any], service: str, mapping: ReplayMapping
) -> TraceSpanPayload | None:
    try:
        trace_id = mapping.to_canonical_trace_id(str(raw["traceId"]).lower())
        span_id = mapping.to_canonical_span_id(str(raw["spanId"]).lower())
        parent = raw.get("parentSpanId") or None
        started_nano = int(raw["startTimeUnixNano"])
        ended_nano = int(raw["endTimeUnixNano"])
        started_at = mapping.to_canonical_time(_from_unix_nano(started_nano))
        duration_ms = (ended_nano - started_nano) / 1_000_000
        attributes = _attributes(raw.get("attributes", []))
        return TraceSpanPayload(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=(
                mapping.to_canonical_span_id(parent.lower()) if parent else None
            ),
            service=service[:128],
            operation=str(raw.get("name", "unknown"))[:128],
            started_at=started_at,
            duration_ms=max(0.0, duration_ms),
            status=_STATUS_BY_CODE.get(raw.get("status", {}).get("code", 0), SpanStatus.UNSET),
            attributes={
                key[:64]: value[:256] for key, value in list(attributes.items())[:20]
            },
        )
    except (KeyError, TypeError, ValueError):
        return None


def _attributes(raw_attributes: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in raw_attributes:
        key = entry.get("key")
        value = entry.get("value", {})
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        for field_name in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if field_name in value:
                values[key] = str(value[field_name])
                break
    return values


def _from_unix_nano(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)
