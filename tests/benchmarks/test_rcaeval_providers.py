from __future__ import annotations

import json

import pytest

from backend.benchmarks.rcaeval.providers import (
    RcaEvalDependencyProvider,
    RcaEvalLogProvider,
    RcaEvalMetricProvider,
    RcaEvalRuntimeStateProvider,
    RcaEvalTraceProvider,
    incident_event_for_case,
)
from backend.diagnosis.evidence_validation import validate_investigation_evidence
from backend.domain.evidence import RuntimeStateValue
from backend.domain.tool_queries import (
    DependencyQuery,
    LogQuery,
    MetricQuery,
    RuntimeStateQuery,
    TraceDirection,
    TraceQuery,
)


def test_log_query_samples_whole_window_and_keeps_late_errors(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        + "2026-01-01T00:00:00Z,worker,received,info\n" * 40
        + "2026-01-01T00:04:00Z,worker,connection reset,error\n"
        + "2026-01-01T00:04:30Z,worker,received,info\n"
        + "2026-01-01T00:06:00Z,other,out of scope,error\n",
        encoding="utf-8",
    )
    provider = RcaEvalLogProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = LogQuery(start_time="2026-01-01T00:00:00Z", end_time="2026-01-01T00:05:00Z",
                     instance="worker", limit=2, reason="Compare logs across the window")
    result = provider.collect(event, query)
    assert result.truncated and result.status == "partial"
    assert [item.payload["message"] for item in result.evidence_items] == [
        "received", "connection reset",
    ]
    from backend.diagnosis.adaptive_tools import project_log_details

    details = project_log_details(result.evidence_items[-1])["sampling"]
    assert details["scan_complete"] is True
    assert details["matching_count"] == 42
    assert details["matched_end"] == "2026-01-01T00:04:30+00:00"
    complete = provider.collect(event, query.model_copy(update={"levels": ["error"]}))
    assert not complete.truncated
    assert len(complete.evidence_items) == 1
    assert complete.evidence_items[0].payload["sampling"]["selection"] == "all_matches"


def test_log_query_reports_incomplete_scan_and_continues_other_files(tmp_path, monkeypatch):
    from backend.benchmarks.rcaeval import providers

    monkeypatch.setattr(providers, "_MAX_SCAN_ROWS", 2)
    (tmp_path / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        + "2026-01-01T00:00:00Z,worker,received,info\n" * 3, encoding="utf-8",
    )
    (tmp_path / "telemetry-01.csv").write_text(
        "time,container_name,message,level\n"
        "2026-01-01T00:04:00Z,worker,timeout,error\n", encoding="utf-8",
    )
    provider = RcaEvalLogProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"),
                              LogQuery(start_time="2026-01-01T00:00:00Z",
                                       end_time="2026-01-01T00:05:00Z", reason="Inspect logs"))
    assert result.truncated
    assert result.error_message.startswith("source_scan_incomplete")
    assert any(item.payload["message"] == "timeout" for item in result.evidence_items)
    assert all(not item.payload["sampling"]["scan_complete"] for item in result.evidence_items)


def test_dependency_query_respects_direction_and_window(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,parentSpanID,serviceName,duration\n"
        + "\n".join(
            f"2026-01-01T00:0{i}:00Z,{1:032x},{i+1:016x},{parent},svc-{i},1000"
            for i, parent in enumerate(["", f"{1:016x}", f"{2:016x}"])
        ), encoding="utf-8",
    )
    provider = RcaEvalDependencyProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = DependencyQuery(
        target="svc-1", direction="downstream", reason="Check the failing edge",
        start_time="2026-01-01T00:00:00Z", end_time="2026-01-01T00:03:00Z",
    )
    downstream = provider.collect(event, query).evidence_items
    assert [(e.payload["source"], e.payload["target"]) for e in downstream] == [("svc-1", "svc-2")]
    upstream = provider.collect(event, DependencyQuery.model_validate({
        **query.model_dump(), "direction": "upstream",
    })).evidence_items
    assert [(e.payload["source"], e.payload["target"]) for e in upstream] == [("svc-0", "svc-1")]
    assert not provider.collect(event, DependencyQuery.model_validate({
        **query.model_dump(), "end_time": "2026-01-01T00:02:00Z",
    })).evidence_items


def test_trace_filters_entities_and_window_before_truncating(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,serviceName,duration\n"
        + "\n".join(
            f"2026-01-01T00:0{i}:00Z,{i+1:032x},{i+1:016x},{service},1000"
            for i, service in enumerate(["frontend", "email", "email", "cart", "email"])
        ), encoding="utf-8",
    )
    provider = RcaEvalTraceProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = TraceQuery(
        entity_ids=["email"], window_start="2026-01-01T00:01:00Z",
        window_end="2026-01-01T00:04:00Z", limit=1,
    )
    result = provider.collect(event, query)
    assert [e.payload["service"] for e in result.evidence_items] == ["email"]
    assert result.truncated
    assert result.evidence_items[0].timestamp.minute == 1
    complete = provider.collect(event, query.model_copy(update={"limit": 2}))
    assert len(complete.evidence_items) == 2
    assert not complete.truncated
    assert not provider.collect(event, query.model_copy(update={"service": "cart"})).evidence_items
    assert provider.collect(event, TraceQuery(direction="upstream")).status == "success"


@pytest.mark.parametrize("aggregation, expected", [
    ("avg", (3, 6)), ("max", (4, 8)), ("sum", (6, 12)),
])
def test_metric_window_and_aggregation_change_evidence(tmp_path, aggregation, expected):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,svc_cpu\n" + "\n".join(
            f"2026-01-01T00:0{i}:00Z,{v}"
            for i, v in enumerate([900, 2, 4, 4, 8, 900])
        ), encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = MetricQuery(
        start_time="2026-01-01T00:01:00Z", end_time="2026-01-01T00:05:00Z",
        aggregation=aggregation, reason="Compare in-window resource values",
    )
    evidence = provider.collect(event, query).evidence_items[0]
    assert (evidence.payload["baseline_value"], evidence.payload["observation_value"]) == expected
    assert evidence.payload["baseline_mean"] == 3
    assert evidence.payload["observation_mean"] == 6
    assert evidence.payload["sample_count"] == 4
    other = provider.collect(event, MetricQuery.model_validate({
        **query.model_dump(), "aggregation": "sum" if aggregation == "avg" else "avg",
    })).evidence_items[0]
    assert other.id != evidence.id


def test_placement_only_runtime_file_does_not_claim_health_or_readiness(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "POD,NODE_NAME\ncheckout-0,node-a\n", encoding="utf-8"
    )
    provider = RcaEvalRuntimeStateProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(
        incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"),
        RuntimeStateQuery(limit=10),
    )

    assert result.evidence_items
    payload = result.evidence_items[0].payload
    assert payload["state"] == RuntimeStateValue.UNKNOWN.value
    assert payload["ready"] is None
    assert "scheduled on" in payload["reason"]


def test_runtime_state_duplicate_rows_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "POD,NODE_NAME\ncheckout-0,node-a\ncheckout-0,node-a\n",
        encoding="utf-8",
    )
    provider = RcaEvalRuntimeStateProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(
        incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"),
        RuntimeStateQuery(limit=10),
    )

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2


def test_runtime_state_filters_health_and_observation_window(tmp_path):
    from datetime import timedelta

    (tmp_path / "telemetry-00.csv").write_text(
        "POD,NODE_NAME,STATE\ncheckout-0,node-a,healthy\ncheckout-1,node-a,unknown\n",
        encoding="utf-8",
    )
    provider = RcaEvalRuntimeStateProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    result = provider.collect(event, RuntimeStateQuery())
    assert [item.payload["entity_id"] for item in result.evidence_items] == ["checkout-1"]
    assert len(provider.collect(event, RuntimeStateQuery(include_healthy=True)).evidence_items) == 2
    healthy = provider.collect(event, RuntimeStateQuery(states=["healthy"]))
    assert [item.payload["entity_id"] for item in healthy.evidence_items] == ["checkout-0"]
    assert not provider.collect(event, RuntimeStateQuery(
        include_healthy=True, window_start=event.started_at - timedelta(minutes=2),
        window_end=event.started_at,
    )).evidence_items


def test_incident_without_traces_uses_telemetry_time(tmp_path):
    from datetime import UTC, datetime

    (tmp_path / "telemetry-00.csv").write_text(
        "time,svc_cpu\n1705602240,1\n", encoding="utf-8",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    assert event.started_at == datetime(2024, 1, 18, 18, 36, tzinfo=UTC)


def test_log_duplicate_rows_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        "2026-01-01T00:00:00Z,checkout,request failed,error\n"
        "2026-01-01T00:00:00Z,checkout,request failed,error\n",
        encoding="utf-8",
    )
    provider = RcaEvalLogProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"))

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2


def test_log_evidence_persists_only_redacted_message(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        '2026-01-01T00:00:00Z,checkout,"Posting Customer: '
        '{""username"":""Alice"",""longNum"":""4111111111111111"",'
        '""ccv"":""123""}",info\n',
        encoding="utf-8",
    )
    provider = RcaEvalLogProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    item = provider.collect(
        incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa")
    ).evidence_items[0]

    persisted = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
    assert "Alice" not in persisted
    assert "4111111111111111" not in persisted
    assert "ccv" in persisted
    assert "[REDACTED]" in persisted


def test_metric_duplicate_series_from_distinct_files_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    content = (
        "time,checkout.requests\n"
        "2026-01-01T00:00:00Z,1\n"
        "2026-01-01T00:01:00Z,1\n"
        "2026-01-01T00:02:00Z,5\n"
        "2026-01-01T00:03:00Z,5\n"
    )
    (case_dir / "telemetry-00.csv").write_text(content, encoding="utf-8")
    (case_dir / "telemetry-01.csv").write_text(content, encoding="utf-8")
    provider = RcaEvalMetricProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"))

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2


def test_metric_limit_keeps_resource_families_under_large_latency_changes(tmp_path):
    columns = ["svc_cpu", "svc_mem", "svc_socket", "svc_diskio"]
    columns += [f"svc_latency-{quantile}" for quantile in (50, 90, 95, 99)]
    (tmp_path / "telemetry-00.csv").write_text(
        "time," + ",".join(columns) + "\n"
        + "\n".join(
            f"2026-01-01T00:0{index}:00Z," + ",".join(
                ["1"] * 8 if index < 2 else ["1.1"] * 4 + ["1000"] * 4
            ) for index in range(4)
        ) + "\n", encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = MetricQuery(
        limit=5, start_time="2026-01-01T00:00:00Z", end_time="2026-01-01T00:04:00Z",
        reason="Compare resource and symptom families",
    )
    result = provider.collect(event, query)
    assert {item.payload["signal_type"] for item in result.evidence_items} == {
        "cpu", "memory", "socket", "disk_io", "latency",
    }
    assert len(result.evidence_items) == 5
    detail = provider.collect(event, query.model_copy(update={
        "limit": 1, "metric_names": ["svc_latency-99"],
    }))
    assert [item.payload["metric"] for item in detail.evidence_items] == ["svc_latency-99"]


@pytest.mark.parametrize("requested, expected", [
    (["cpu_usage", "memory_usage", "disk_io", "connection_count"],
     {"svc_cpu", "svc_mem", "svc_diskio", "svc_socket"}),
    (["svc_memory"], {"svc_mem"}),
    (["memory"], {"svc_mem"}),
    (["svc_container-memory-mapped-file"], {"svc_container-memory-mapped-file"}),
    (["nonexistent_metric"], set()),
])
def test_metric_query_aliases_resolve_columns_without_losing_exact_raw_queries(
    tmp_path, requested, expected,
):
    columns = ["svc_cpu", "svc_mem", "svc_diskio", "svc_socket",
               "svc_container-memory-mapped-file", "other_cpu"]
    (tmp_path / "telemetry-00.csv").write_text(
        "time," + ",".join(columns) + "\n"
        + "\n".join(f"2026-01-01T00:0{i}:00Z," + ",".join([str(i+1)] * 6)
                    for i in range(4)) + "\n", encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    query = MetricQuery(
        metric_names=requested, instance="svc", limit=10,
        start_time="2026-01-01T00:00:00Z", end_time="2026-01-01T00:04:00Z",
        reason="Resolve resource metrics",
    )
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"), query)
    assert {item.payload["metric"] for item in result.evidence_items} == expected


def test_metric_provider_prefers_service_signal_columns_over_raw_container_noise(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "time,svc_container-memory-mapped-file,svc_cpu,svc_socket\n"
        "2026-01-01T00:00:00Z,0,1,1\n"
        "2026-01-01T00:01:00Z,0,1,1\n"
        "2026-01-01T00:02:00Z,1000000000,10,2\n"
        "2026-01-01T00:03:00Z,1000000000,10,2\n",
        encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"))
    metrics = {item.payload["metric"] for item in result.evidence_items}
    signal_types = {
        item.payload["metric"]: item.payload["signal_type"]
        for item in result.evidence_items
    }

    assert metrics == {"svc_cpu", "svc_socket"}
    assert signal_types == {"svc_cpu": "cpu", "svc_socket": "socket"}


def test_metric_details_are_discoverable_scoped_and_preserve_source_values(tmp_path):
    from backend.diagnosis.adaptive_tools import project_tool_evidence
    from backend.diagnosis.v11_runtime import _live_evidence_prompt_projection

    columns = ["svc_cpu", "svc_container-cpu-user-seconds-total",
               "svc_container-cpu-system-seconds-total", "other_container-cpu-user-seconds-total"]
    values = [(1, 0.8, 0.2, 9), (2, 1.8, 0.2, 9), (10, 1, 9, 9), (12, 1, 11, 9)]
    (tmp_path / "telemetry-00.csv").write_text(
        "time," + ",".join(columns) + "\n" + "\n".join(
            f"2026-01-01T00:0{i}:00Z," + ",".join(map(str, row))
            for i, row in enumerate(values)
        ), encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    overview = provider.collect(event).evidence_items[0]
    related = overview.payload["related_metric_names"]
    assert set(related) == set(columns[1:3])
    compact = _live_evidence_prompt_projection(overview)
    assert compact["sampling_interval_seconds"] == 60
    assert compact["max_sample_gap_seconds"] == 60
    assert compact["sample_count"] == 4
    assert "unit" not in compact
    assert {p["metric"] for p in compact["related_observations"]} == set(related)
    assert len(compact["time_profile"]) == 4
    assert "related_metric_names" not in compact
    query = MetricQuery(
        metric_names=related, start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:04:00Z", reason="Compare user and system CPU",
    )
    details = provider.collect(event, query).evidence_items
    projected = {item["metric"]: item for item in project_tool_evidence(details)}
    user = projected[columns[1]]
    system = projected[columns[2]]
    assert user["baseline_mean"] == pytest.approx(1.3)
    assert user["observation_mean"] == 1
    assert system["observation_mean"] == 10
    assert [p["mean"] for p in system["time_profile"]] == [0.2, 0.2, 9, 11]
    assert "do not infer a rate" in system["value_semantics"]
    assert "other_" not in json.dumps(projected)


def test_retry_namespace_keeps_rcaeval_evidence_ids_globally_unique(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        "2026-01-01T00:00:00Z,checkout,request failed,error\n",
        encoding="utf-8",
    )
    event = incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa")
    first = RcaEvalLogProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    ).collect(event)
    retry = RcaEvalLogProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-2",
    ).collect(event)

    assert first.evidence_items[0].id != retry.evidence_items[0].id
    assert (
        first.evidence_items[0].id
        == RcaEvalLogProvider(
            case_dir,
            case_id="re2-aaaaaaaaaaaaaaaa",
            runtime_manifest_hash="a" * 64,
            evidence_namespace="run-1",
        )
        .collect(event)
        .evidence_items[0]
        .id
    )


@pytest.mark.parametrize("timestamp", [
    "1705602240", "1705602240000", "1705602240000000", "1705602240000000000",
])
def test_numeric_timestamps_preserve_observation_time(timestamp):
    from datetime import UTC, datetime

    from backend.benchmarks.rcaeval.providers import _cell_timestamp

    assert _cell_timestamp(timestamp, datetime(2026, 1, 1, tzinfo=UTC)) == datetime(
        2024, 1, 18, 18, 24, tzinfo=UTC,
    )


def test_trace_direction_keeps_trace_identity_and_window(tmp_path):
    rows = []
    for trace, prefix in [(1, "a"), (2, "b")]:
        for span, parent, service, minute in [
            (1, "", "caller", 0), (2, f"{1:016x}", "target", 1),
            (3, f"{2:016x}", "child", 2), (4, f"{3:016x}", "late", 4),
        ]:
            rows.append(f"2026-01-01T00:0{minute}:00Z,{trace:032x},{span:016x},"
                        f"{parent},{prefix}-{service},1000")
    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,parentSpanID,serviceName,duration\n" + "\n".join(rows),
        encoding="utf-8",
    )
    provider = RcaEvalTraceProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    query = TraceQuery(service="a-target", direction="downstream",
                       window_start="2026-01-01T00:00:00Z",
                       window_end="2026-01-01T00:03:00Z")
    down = provider.collect(event, query)
    assert {e.payload["service"] for e in down.evidence_items} == {"a-target", "a-child"}
    up = provider.collect(event, query.model_copy(update={"direction": TraceDirection.UPSTREAM}))
    assert {e.payload["service"] for e in up.evidence_items} == {"a-target", "a-caller"}


def test_trace_sampling_includes_late_slow_span(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,serviceName,duration\n" + "\n".join(
            f"2026-01-01T00:{i:02d}:00Z,{i+1:032x},{i+1:016x},svc,{9000000 if i == 19 else 1000}"
            for i in range(20)
        ), encoding="utf-8",
    )
    provider = RcaEvalTraceProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"),
                              TraceQuery(limit=4))
    assert result.truncated
    assert len(result.evidence_items) == 4
    assert any(e.timestamp.minute == 19 for e in result.evidence_items)
    assert any(e.timestamp.minute < 10 for e in result.evidence_items)
    assert "duration/time samples" in result.error_message
    assert "query_population" in result.error_message


def test_resource_components_share_query_window_and_preserve_controls(tmp_path):
    columns = ["svc_mem", "svc_container-memory-cache", "svc_container-memory-rss",
               "svc_workload", "other_container-memory-cache"]
    (tmp_path / "telemetry-00.csv").write_text(
        "time," + ",".join(columns) + "\n" + "\n".join(
            f"2026-01-01T00:0{i}:00Z,{total},{cache},10,2,999"
            for i, (total, cache) in enumerate([(999, 999), (10, 0), (10, 0),
                                               (50, 40), (70, 60), (999, 999)])
        ), encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    event = incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    result = provider.collect(event, MetricQuery(
        metric_names=["svc_mem"], start_time="2026-01-01T00:01:00Z",
        end_time="2026-01-01T00:05:00Z", reason="Compare memory components",
    ))
    assert len(result.evidence_items) == 1
    related = {p["metric"]: (p["baseline_mean"], p["observation_mean"])
               for p in result.evidence_items[0].payload["related_observations"]}
    assert related == {"svc_container-memory-cache": (0, 50),
                       "svc_container-memory-rss": (10, 10), "svc_workload": (2, 2)}


def test_trace_malformed_extreme_sample_does_not_hide_valid_spans(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,serviceName,duration\n"
        "2026-01-01T00:00:00Z,invalid,invalid,svc,9999999\n"
        f"2026-01-01T00:01:00Z,{1:032x},{1:016x},svc,1000\n"
        f"2026-01-01T00:02:00Z,{2:032x},{2:016x},svc,2000\n",
        encoding="utf-8",
    )
    provider = RcaEvalTraceProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"),
                              TraceQuery(limit=2))
    assert len(result.evidence_items) == 2
    assert {e.payload["trace_id"] for e in result.evidence_items} == {f"{1:032x}", f"{2:032x}"}


def test_disk_overview_includes_cpu_memory_and_demand_controls(tmp_path):
    from backend.diagnosis.adaptive_tools import project_metric_details

    columns = ["svc_diskio", "svc_container-fs-reads-bytes-total",
               "svc_container-fs-writes-bytes-total", "svc_container-cpu-user-seconds-total",
               "svc_container-cpu-system-seconds-total", "svc_container-memory-cache",
               "svc_container-memory-rss", "svc_workload", "svc_latency-90"]
    (tmp_path / "telemetry-00.csv").write_text(
        "time," + ",".join(columns) + "\n" + "\n".join(
            f"2026-01-01T00:0{i}:00Z," + ",".join(map(str, row))
            for i, row in enumerate([(0, 0, 0, 1, 1, 0, 10, 2, 1)] * 2
                                    + [(100, 50, 50, 2, 8, 90, 10, 2, 3)] * 2)
        ), encoding="utf-8",
    )
    provider = RcaEvalMetricProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"),
                              MetricQuery(metric_names=["svc_diskio"],
                                          start_time="2026-01-01T00:00:00Z",
                                          end_time="2026-01-01T00:04:00Z",
                                          reason="Check resource components"))
    projection = project_metric_details(result.evidence_items[0], compact=True)
    related = {p["metric"]: p for p in projection["related_observations"]}
    assert set(related) == set(columns[1:])
    assert related["svc_container-memory-rss"]["observation_mean"] == 10
    assert related["svc_container-memory-cache"]["observation_mean"] == 90
    assert related["svc_workload"]["baseline_mean"] == related["svc_workload"]["observation_mean"]


def test_trace_query_keeps_child_wait_facts_when_result_limit_hides_children(tmp_path):
    from backend.diagnosis.adaptive_tools import project_trace_details

    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,parentSpanID,serviceName,operationName,duration\n"
        f"2026-01-01T00:00:00Z,{1:032x},{1:016x},,worker,Serve,1000000\n"
        f"2026-01-01T00:00:00.010Z,{1:032x},{2:016x},{1:016x},worker,rpc/Call,950000\n"
        f"2026-01-01T00:00:00.100Z,{1:032x},{3:016x},{2:016x},remote,rpc/Call,1000\n",
        encoding="utf-8",
    )
    provider = RcaEvalTraceProvider(tmp_path, case_id="re2-aaaaaaaaaaaaaaaa",
                                  runtime_manifest_hash="a" * 64, evidence_namespace="run-1")
    result = provider.collect(incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa"),
                              TraceQuery(service="worker", operation="Serve", limit=1))
    assert len(result.evidence_items) == 1
    timing = project_trace_details(result.evidence_items[0])["child_timing"]
    assert timing["covered_ms"] == 950
    assert timing["uncovered_ms"] == 50
    assert "NOT CPU self-time" in timing["semantics"]


def test_trace_snapshot_is_shared_immutable_and_scoped_to_provider_set(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from backend.benchmarks.rcaeval.providers import build_rcaeval_providers

    (tmp_path / "telemetry-00.csv").write_text(
        "time,traceID,spanID,serviceName,duration\n"
        f"2026-01-01T00:00:00Z,{1:032x},{1:016x},svc,1000\n", encoding="utf-8",
    )
    providers = build_rcaeval_providers(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )
    telemetry = providers[0].telemetry
    assert all(p.telemetry is telemetry for p in providers)
    with ThreadPoolExecutor(3) as pool:
        snapshots = list(pool.map(lambda _: telemetry.trace_rows(), range(3)))
    assert all(rows is snapshots[0] for rows in snapshots)
    with pytest.raises(TypeError):
        snapshots[0][0][1]["serviceName"] = "changed"
    independent = RcaEvalTraceProvider(
        tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
        evidence_namespace="run-2",
    )
    assert independent.telemetry.trace_rows() is not snapshots[0]
