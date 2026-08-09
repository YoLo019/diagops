"""TempoTraceProvider 契约与 canonical 投影（fake HTTP，不接触 Docker/网络）。"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, SpanStatus
from backend.domain.tool_queries import TraceDirection, TraceQuery
from backend.providers.local_package import FileTraceProvider
from backend.providers.results import ProviderStatus
from backend.providers.tempo import (
    ReplayMapping,
    TempoTraceProvider,
    _project_otlp_trace,
)
from backend.services.offline_tool_acceptance import build_synthetic_package
from backend.services.tempo_acceptance import (
    build_otlp_export,
    build_replay_mapping,
    compare_trace_results,
)

_STARTED = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.WEBHOOK,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="checkout 5xx",
        description="synthetic",
        started_at=_STARTED,
    )


@pytest.fixture
def package(tmp_path):
    return build_synthetic_package(tmp_path / "package")


def _canonical_spans(package):
    rows = json.loads((package.root / "spans.json").read_text(encoding="utf-8"))
    return rows


def test_mapping_roundtrip():
    mapping = ReplayMapping(
        offset_ms=1000,
        trace_id_map={"a" * 32: "b" * 32},
        span_id_map={"1" * 16: "2" * 16},
    )

    assert mapping.to_backend_trace_id("a" * 32) == "b" * 32
    assert mapping.to_canonical_trace_id("b" * 32) == "a" * 32
    assert mapping.to_canonical_span_id("2" * 16) == "1" * 16
    assert mapping.to_canonical_time(mapping.to_backend_time(_STARTED)) == _STARTED
    # 未映射的 ID 原样通过（恒等映射用于无 replay 场景）。
    assert mapping.to_backend_trace_id("f" * 32) == "f" * 32


def test_otlp_projection_applies_inverse_mapping():
    mapping = ReplayMapping(
        offset_ms=60_000,
        trace_id_map={"a" * 32: "c" * 32},
        span_id_map={"1" * 16: "d" * 16, "2" * 16: "e" * 16},
    )
    backend_start = mapping.to_backend_time(_STARTED)
    start_nano = int(backend_start.timestamp() * 1_000_000_000)
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "payment-service"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "c" * 32,
                                "spanId": "d" * 16,
                                "parentSpanId": "e" * 16,
                                "name": "POST /pay",
                                "startTimeUnixNano": str(start_nano),
                                "endTimeUnixNano": str(start_nano + 5_000_000),
                                "status": {"code": 2},
                                "attributes": [
                                    {"key": "http.status_code", "value": {"intValue": 504}}
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    spans = _project_otlp_trace(trace, mapping)

    assert len(spans) == 1
    span = spans[0]
    assert span.trace_id == "a" * 32
    assert span.span_id == "1" * 16
    assert span.parent_span_id == "2" * 16
    assert span.started_at == _STARTED
    assert span.duration_ms == pytest.approx(5.0)
    assert span.status == SpanStatus.ERROR
    assert span.attributes == {"http.status_code": "504"}


def test_otlp_projection_marks_malformed_spans():
    trace = {
        "batches": [
            {"resource": {"attributes": []}, "scopeSpans": [{"spans": [{"bad": True}]}]}
        ]
    }

    assert _project_otlp_trace(trace, ReplayMapping()) == [None]


def _mock_tempo_client(export_payload: dict, *, search_ids: list[str]) -> httpx.Client:
    # replay 请求体是 {"resourceSpans": [...]}；Tempo 查询响应包装为 {"batches": [...]}。
    trace_response = {"batches": export_payload["resourceSpans"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(
                200, json={"traces": [{"traceID": item} for item in search_ids]}
            )
        if request.url.path.startswith("/api/traces/"):
            return httpx.Response(200, json=trace_response)
        return httpx.Response(404, json={})

    return httpx.Client(
        base_url="http://tempo.test", transport=httpx.MockTransport(handler)
    )


def test_tempo_provider_matches_file_provider_offline(package):
    rows = _canonical_spans(package)
    from backend.domain.evidence import TraceSpanPayload

    spans = [TraceSpanPayload.model_validate(row) for row in rows]
    mapping = build_replay_mapping(spans)
    export = build_otlp_export(spans, mapping)
    backend_ids = sorted({mapping.to_backend_trace_id(span.trace_id) for span in spans})
    client = _mock_tempo_client(export, search_ids=backend_ids)
    tempo = TempoTraceProvider("http://tempo.test", mapping=mapping, client=client)
    file_provider = FileTraceProvider(package)
    event = _event()
    query = TraceQuery(
        window_start=_STARTED - timedelta(minutes=30),
        window_end=_STARTED + timedelta(minutes=30),
    )

    file_result = file_provider.collect(event, query)
    tempo_result = tempo.collect(event, query)

    assert tempo_result.status == ProviderStatus.SUCCESS
    assert compare_trace_results(file_result, tempo_result) == []


def test_tempo_provider_parity_with_direction_query(package):
    from backend.domain.evidence import TraceSpanPayload

    rows = _canonical_spans(package)
    spans = [TraceSpanPayload.model_validate(row) for row in rows]
    mapping = build_replay_mapping(spans)
    export = build_otlp_export(spans, mapping)
    backend_ids = sorted({mapping.to_backend_trace_id(span.trace_id) for span in spans})
    client = _mock_tempo_client(export, search_ids=backend_ids)
    tempo = TempoTraceProvider("http://tempo.test", mapping=mapping, client=client)
    file_provider = FileTraceProvider(package)
    event = _event()
    query = TraceQuery(
        service="payment-service",
        direction=TraceDirection.UPSTREAM,
        window_start=_STARTED - timedelta(minutes=30),
        window_end=_STARTED + timedelta(minutes=30),
    )

    mismatches = compare_trace_results(
        file_provider.collect(event, query), tempo.collect(event, query)
    )

    assert mismatches == []
    kinds = {
        item.kind
        for item in tempo.collect(event, query).evidence_items
    }
    assert EvidenceKind.TRACE_ERROR in kinds


def test_tempo_provider_timeout_is_failed_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow tempo")

    client = httpx.Client(
        base_url="http://tempo.test", transport=httpx.MockTransport(handler)
    )
    tempo = TempoTraceProvider("http://tempo.test", client=client)

    result = tempo.collect(_event(), TraceQuery())

    assert result.status == ProviderStatus.FAILED
    assert result.error_message == "provider timeout"


def test_tempo_provider_http_error_is_failed_not_mock():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(
        base_url="http://tempo.test", transport=httpx.MockTransport(handler)
    )
    tempo = TempoTraceProvider("http://tempo.test", client=client)

    result = tempo.collect(_event(), TraceQuery())

    assert result.status == ProviderStatus.FAILED
    assert result.error_message == "provider collection failed"
    assert result.evidence_items == []


def test_tempo_provider_trace_id_query_uses_backend_mapping():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"batches": []})

    client = httpx.Client(
        base_url="http://tempo.test", transport=httpx.MockTransport(handler)
    )
    mapping = ReplayMapping(trace_id_map={"a" * 32: "b" * 32})
    tempo = TempoTraceProvider("http://tempo.test", mapping=mapping, client=client)

    result = tempo.collect(_event(), TraceQuery(trace_id="a" * 32))

    assert result.status == ProviderStatus.SUCCESS
    assert requested == [f"/api/traces/{'b' * 32}"]
