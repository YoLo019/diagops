from __future__ import annotations

import csv
import json

import pytest

from backend.benchmarks.rcaeval import providers as module
from backend.diagnosis.adaptive_tools import project_tool_evidence
from backend.domain.tool_queries import DependencyQuery, LogQuery, MetricQuery, TraceQuery


def provider(tmp_path, kind):
    return kind(tmp_path, case_id="re2-aaaaaaaaaaaaaaaa", runtime_manifest_hash="a" * 64,
                evidence_namespace="test-run")


def traces(tmp_path, rows):
    with (tmp_path / "telemetry-00.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "traceID", "spanID", "parentSpanID", "serviceName",
                         "operationName", "duration", "statusCode"])
        writer.writerows(rows)
    return module.incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")


@pytest.mark.parametrize("operation,code,status", [
    ("shop.API/Read", "14.0", "error"), ("shop.API/Read", "0.0", "ok"),
    ("shop.API/Read", "2.0", "error"), ("shop.API/Read", "", "unset"),
    ("GET /items", "200.0", "ok"), ("GET /items", "503.0", "error"),
    ("GET /items", "14.0", "unset"), ("unspecified", "2.0", "error"),
])
def test_status_codes_respect_source_operation(tmp_path, operation, code, status):
    event = traces(tmp_path, [["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "",
                              "caller", operation, 1000, code]])
    p = provider(tmp_path, module.RcaEvalTraceProvider)
    result = p.collect(event)
    assert result.evidence_items[0].payload["status"] == status
    assert bool(p.collect(event, TraceQuery(error_only=True)).evidence_items) == (status == "error")


def test_log_error_is_searchable_redacted_and_survives_projection(tmp_path):
    with (tmp_path / "telemetry-00.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "container_name", "message", "level", "error"])
        for i in range(30):
            writer.writerow(["2026-01-01T00:00:00Z", "caller", f"normal {i}", "info", ""])
        writer.writerow(["2026-01-01T00:01:00Z", "caller", "request failed", "warning",
                         "rpc Unavailable: connection reset; token=private-secret"])
    p = provider(tmp_path, module.RcaEvalLogProvider)
    event = module.incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    initial = project_tool_evidence(p.collect(event).evidence_items)
    assert "connection reset" in initial[0]["error"]
    result = p.collect(event, LogQuery(
        instance="caller", keywords=["Unavailable"], start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:02:00Z", reason="Inspect RPC failures",
    ))
    rendered = json.dumps(project_tool_evidence(result.evidence_items))
    assert "connection reset" in rendered
    assert "private-secret" not in rendered
    assert len(result.evidence_items) == 1


def test_scan_cap_cannot_report_successful_absence(tmp_path, monkeypatch):
    event = traces(tmp_path, [[f"2026-01-01T00:0{i}:00Z", f"{i+1:032x}", f"{i+1:016x}",
                              "", "api", "shop.API/Read", 1000, "14.0"] for i in range(4)])
    monkeypatch.setattr(module, "_MAX_TRACE_SCAN_ROWS", 2)
    result = provider(tmp_path, module.RcaEvalTraceProvider).collect(event, TraceQuery(
        window_start="2026-01-01T00:02:00Z", window_end="2026-01-01T00:04:00Z",
    ))
    assert result.evidence_items == []
    assert result.status == "partial" and result.truncated
    assert "source_scan_incomplete" in result.error_message
    dependencies = provider(tmp_path, module.RcaEvalDependencyProvider).collect(event)
    assert dependencies.status == "partial"


def test_paired_rpc_distinguishes_caller_wait_from_slow_server(tmp_path):
    event = traces(tmp_path, [
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "", "caller",
         "shop.API/Read", 900000, "0.0"],
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{2:016x}", f"{1:016x}", "api",
         "shop.API/Read", 1000, "0.0"],
        ["2026-01-01T00:02:00Z", f"{2:032x}", f"{1:016x}", "", "caller",
         "shop.API/Read", 900000, "0.0"],
        ["2026-01-01T00:02:00Z", f"{2:032x}", f"{2:016x}", f"{1:016x}", "api",
         "shop.API/Read", 890000, "0.0"],
    ])
    result = provider(tmp_path, module.RcaEvalTraceProvider).collect(
        event, TraceQuery(service="caller"))
    callers = [p for p in project_tool_evidence(result.evidence_items)
               if p["service"] == "caller"]
    assert {p["trace_details"]["rpc.duration_difference_ms"] for p in callers} == {
        "899.000", "10.000",
    }
    dep = provider(tmp_path, module.RcaEvalDependencyProvider).collect(event, DependencyQuery(
        target="caller", start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:03:00Z", reason="Compare both ends",
    )).evidence_items[0]
    assert dep.payload["caller_mean_duration_ms"] == 900
    assert dep.payload["callee_mean_duration_ms"] == 445.5
    assert dep.payload["window_start"] == "2026-01-01T00:00:00+00:00"
    assert "whole-window aggregate" in dep.summary
    assert "baseline and incident may be mixed" in dep.payload["aggregation_semantics"]


def test_metric_comparison_split_is_not_deviation_time(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,worker_mem\n" + "\n".join(
            f"2026-01-01T00:{i:02d}:00Z,{value}"
            for i, value in enumerate([10] * 7 + [20] * 3)
        ), encoding="utf-8",
    )
    event = module.incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    item = provider(tmp_path, module.RcaEvalMetricProvider).collect(event, MetricQuery(
        start_time="2026-01-01T00:00:00Z", end_time="2026-01-01T00:10:00Z", reason="Time order",
    )).evidence_items[0]
    projected = project_tool_evidence([item])[0]
    assert "not an observed failure onset" in projected["timestamp_semantics"]
    assert projected["split_at"].endswith("00:05:00+00:00")
    assert projected["first_sustained_deviation"]["timestamp"].endswith("00:07:00+00:00")


def test_dependency_missing_status_is_explicitly_unknown(tmp_path):
    event = traces(tmp_path, [
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "", "caller",
         "shop.API/Read", 900000, ""],
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{2:016x}", f"{1:016x}", "api",
         "shop.API/Read", 1000, ""],
    ])
    item = provider(tmp_path, module.RcaEvalDependencyProvider).collect(event).evidence_items[0]
    assert item.payload["errors"] == 0
    assert item.payload["callee_unknown_status"] == 1
    assert item.payload["caller_unknown_status"] == 1
    assert "callee_unknown=1" in item.summary
    assert "caller_unknown=1" in item.summary


@pytest.mark.parametrize("ambiguous", [False, True])
def test_source_service_alias_requires_unambiguous_observed_names(tmp_path, ambiguous):
    (tmp_path / "telemetry-01.csv").write_text("time,web_cpu\n", encoding="utf-8")
    rows = [["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "", "webservice",
             "shop.API/Read", 1000, "0.0"]]
    if ambiguous:
        rows.append(["2026-01-01T00:01:00Z", f"{2:032x}", f"{2:016x}", "", "web",
                     "shop.API/Read", 1000, "0.0"])
    event = traces(tmp_path, rows)
    result = provider(tmp_path, module.RcaEvalTraceProvider).collect(event)
    services = {e.payload["service"] for e in result.evidence_items}
    assert services == ({"web", "webservice"} if ambiguous else {"web"})
    if not ambiguous:
        assert result.evidence_items[0].payload["attributes"]["source_service"] == "webservice"


def test_rpc_pair_does_not_cross_trace_or_operation(tmp_path):
    event = traces(tmp_path, [
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "", "caller",
         "shop.API/Read", 900000, "0.0"],
        ["2026-01-01T00:01:00Z", f"{1:032x}", f"{2:016x}", f"{1:016x}", "api",
         "shop.API/Write", 1000, "0.0"],
        ["2026-01-01T00:01:00Z", f"{2:032x}", f"{3:016x}", f"{1:016x}", "api",
         "shop.API/Read", 1000, "0.0"],
    ])
    items = provider(tmp_path, module.RcaEvalTraceProvider).collect(event).evidence_items
    assert all("rpc.peer_duration_ms" not in item.payload["attributes"] for item in items)


def test_exact_source_scan_limit_keeps_complete_status(tmp_path, monkeypatch):
    event = traces(tmp_path, [["2026-01-01T00:01:00Z", f"{1:032x}", f"{1:016x}", "",
                              "api", "shop.API/Read", 1000, "0.0"]])
    monkeypatch.setattr(module, "_MAX_TRACE_SCAN_ROWS", 1)
    result = provider(tmp_path, module.RcaEvalTraceProvider).collect(event)
    assert result.status == "success"
    assert not result.truncated


def test_metric_single_spike_does_not_become_sustained_deviation(tmp_path):
    (tmp_path / "telemetry-00.csv").write_text(
        "time,worker_mem\n" + "\n".join(
            f"2026-01-01T00:{i:02d}:00Z,{value}"
            for i, value in enumerate([10] * 5 + [20, 10, 10, 10, 10])
        ), encoding="utf-8",
    )
    event = module.incident_event_for_case(tmp_path, "re2-aaaaaaaaaaaaaaaa")
    item = provider(tmp_path, module.RcaEvalMetricProvider).collect(event).evidence_items[0]
    assert not item.payload["first_sustained_deviation"]
    assert "first_sustained_deviation" not in project_tool_evidence([item])[0]
