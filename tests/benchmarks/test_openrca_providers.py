import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.benchmarks.openrca.models import OpenRcaPartition, OpenRcaRuntimeCase
from backend.benchmarks.openrca.providers import (
    OpenRcaDependencyProvider,
    OpenRcaLogProvider,
    OpenRcaMetricProvider,
    _is_counter_like_name,
    _normalize_counter_like,
)
from backend.diagnosis.signal_semantics import SeriesPoint
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind
from backend.domain.tool_queries import DependencyQuery, LogQuery, MetricQuery
from backend.providers.results import ProviderStatus

TZ = timezone(timedelta(hours=8))


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "openrca"


def runtime_case(partition: str) -> OpenRcaRuntimeCase:
    return OpenRcaRuntimeCase(
        case_id=f"{partition}:1",
        partition=OpenRcaPartition(partition),
        row_id="1",
        task_index="task_1",
        instruction="fabricated incident",
        start_time=datetime(2026, 7, 14, 12, tzinfo=TZ),
        end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
        telemetry_dir=f"{partition}/telemetry/2026-07-14",
    )


def event(partition: str) -> IncidentEvent:
    case = runtime_case(partition)
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkoutservice",
        environment="openrca",
        severity=Severity.CRITICAL,
        title="OpenRCA fixture",
        description="fabricated benchmark incident",
        started_at=case.start_time,
        time_window_minutes=15,
    )


def dependency_dataset(
    tmp_path: Path,
    samples: list[tuple[str, int, int]],
    *,
    child_component: str = "paymentservice",
) -> Path:
    directory = tmp_path / "Bank" / "telemetry" / "2026-07-14"
    directory.mkdir(parents=True)
    rows = ["traceId,id,pid,serviceName,startTime,elapsedTime,status_code"]
    for index, (timestamp, duration, status) in enumerate(samples):
        rows.extend(
            [
                f"trace-{index},parent-{index},,checkoutservice,{timestamp},10,200",
                (
                    f"trace-{index},child-{index},parent-{index},{child_component},"
                    f"{timestamp},{duration},{status}"
                ),
            ]
        )
    (directory / "traces.csv").write_text("\n".join(rows), encoding="utf-8")
    return tmp_path


def metric_dataset(tmp_path: Path, rows: str, component_dir: str = "Bank") -> Path:
    directory = tmp_path / component_dir / "telemetry" / "2026-07-14"
    directory.mkdir(parents=True)
    (directory / "metrics.csv").write_text(rows, encoding="utf-8")
    return tmp_path


def metric_query() -> MetricQuery:
    return MetricQuery(
        start_time=datetime(2026, 7, 14, 11, 55, tzinfo=TZ),
        end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
        reason="inspect anomalous metrics",
        limit=10,
        metric_names=[],
    )


def baseline_csv(component: str, metric_name: str, value: float) -> str:
    """紧邻查询窗口的等长前置窗口（11:40-11:55）内的正常基线点。"""
    return "".join(
        f"2026-07-14T11:{minute}:00+08:00,{component},{metric_name},{value}\n"
        for minute in ("42", "47", "52")
    )


@pytest.mark.parametrize("partition", ["Bank", "Telecom", "Market/cloudbed-1"])
def test_metric_provider_normalizes_schema_variants(partition: str, fixture_root: Path):
    provider = OpenRcaMetricProvider(fixture_root, runtime_case(partition))

    result = provider.collect(event(partition), metric_query())

    assert result.status == ProviderStatus.SUCCESS
    payload = result.evidence_items[0].payload
    assert result.evidence_items[0].kind == EvidenceKind.METRIC_TREND
    assert payload["component"]
    assert payload["signal_name"]
    assert payload["deviation_score"] >= 1
    assert payload["normalized_strength"] <= 10
    assert payload["anomaly_segment_id"]
    assert "root_cause_claims" not in payload
    assert "telemetry" not in str(payload).lower()


def test_metric_provider_applies_limit_to_distinct_series(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("bank-api", "noisy_metric", 10)
        + baseline_csv("bank-api", "memory_usage", 10)
        + "2026-07-14T12:01:00+08:00,bank-api,noisy_metric,1000\n"
        + "2026-07-14T12:02:00+08:00,bank-api,noisy_metric,900\n"
        + "2026-07-14T12:03:00+08:00,bank-api,memory_usage,100\n"
    )
    dataset = metric_dataset(tmp_path, rows)
    query = metric_query().model_copy(update={"limit": 2})

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), query
    )

    assert {item.payload["signal_name"] for item in result.evidence_items} == {
        "noisy_metric",
        "memory_usage",
    }


def test_metric_provider_does_not_classify_istio_as_disk_io(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("frontend", "istio_request_duration_milliseconds", 10)
        + "2026-07-14T12:03:00+08:00,frontend,istio_request_duration_milliseconds,100\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.evidence_items[0].payload["signal_type"] == "latency"


def test_metric_provider_classifies_container_network_drop_as_corruption(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("node-1.bank-api-0", "container_network_receive_packets_dropped", 0)
        + (
            "2026-07-14T12:03:00+08:00,node-1.bank-api-0,"
            "container_network_receive_packets_dropped,40\n"
        )
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.evidence_items[0].payload["signal_type"] == "network_corruption"


def test_metric_provider_maps_tcp_wait_to_network_latency(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("bank-api", "tcp_time_wait", 10)
        + "2026-07-14T12:03:00+08:00,bank-api,tcp_time_wait,500\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.evidence_items[0].payload["signal_type"] == "network_latency"


def test_metric_provider_family_coverage_keeps_memory_family(tmp_path: Path):
    # 零基线 latency 的原始 deviation 曾达到 1e9 量级并挤出 memory family；
    # family-first 选择保证 memory 覆盖。V10.1 起零基线信号为 presence-only
    # strength 1，不伪造幅度置信度，family 覆盖语义不变。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("frontend", "request_duration", 0)
        + baseline_csv("checkoutservice", "memory_usage", 10)
        + "2026-07-14T12:01:00+08:00,frontend,request_duration,5\n"
        + "2026-07-14T12:02:00+08:00,checkoutservice,memory_usage,15\n"
    )
    dataset = metric_dataset(tmp_path, rows)
    provider = OpenRcaMetricProvider(dataset, runtime_case("Bank"))

    top = provider.collect(event("Bank"), metric_query().model_copy(update={"limit": 1}))
    both = provider.collect(event("Bank"), metric_query().model_copy(update={"limit": 2}))

    assert top.evidence_items[0].payload["signal_type"] == "memory"
    by_name = {item.payload["signal_name"]: item.payload for item in both.evidence_items}
    assert by_name["request_duration"]["normalized_strength"] == 1.0
    assert by_name["request_duration"]["strength_basis"] == "presence_only"
    assert by_name["request_duration"]["anomaly_point_count"] == 1
    assert by_name["memory_usage"]["normalized_strength"] == 5
    assert by_name["memory_usage"]["strength_basis"] == "relative"


def test_metric_provider_zero_baseline_sustained_anomaly_keeps_point_count(
    tmp_path: Path,
):
    # 零基线持续异常不丢失 Evidence；point count 为 segment 内有限异常点数。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("bank-api", "process_restarts", 0)
        + "2026-07-14T12:01:00+08:00,bank-api,process_restarts,1\n"
        + "2026-07-14T12:02:00+08:00,bank-api,process_restarts,2\n"
        + "2026-07-14T12:03:00+08:00,bank-api,process_restarts,3\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert len(result.evidence_items) == 1
    payload = result.evidence_items[0].payload
    assert payload["strength_basis"] == "presence_only"
    assert payload["normalized_strength"] == 1.0
    assert payload["deviation_score"] == 1.0
    assert payload["anomaly_point_count"] == 3


def test_metric_provider_preserves_component_hierarchy(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("node-6.checkoutservice-0", "memory_usage", 10)
        + "2026-07-14T12:03:00+08:00,node-6.checkoutservice-0,memory_usage,50\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    payload = result.evidence_items[0].payload
    assert payload["component"] == "node-6.checkoutservice-0"
    assert payload["node"] == "node-6"
    assert payload["service"] == "checkoutservice"
    assert payload["instance"] == "checkoutservice-0"


def test_metric_provider_uses_segment_onset_not_peak_timestamp(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("bank-api", "cpu_usage", 10)
        + "2026-07-14T12:01:00+08:00,bank-api,cpu_usage,12\n"
        + "2026-07-14T12:02:00+08:00,bank-api,cpu_usage,95\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    item = result.evidence_items[0]
    assert item.timestamp == datetime(2026, 7, 14, 12, 1, tzinfo=TZ)
    assert item.payload["anomaly_onset"] == item.timestamp.isoformat()
    assert item.payload["anomaly_ended_at"] == datetime(
        2026, 7, 14, 12, 2, tzinfo=TZ
    ).isoformat()
    assert item.payload["anomaly_segment_id"]
    assert item.payload["normalized_strength"] == 10
    assert item.payload["deviation_score"] == 10
    assert item.payload["baseline_value"] == 10
    assert item.payload["strength_basis"] == "relative"
    assert item.payload["anomaly_point_count"] == 2


def test_metric_provider_without_baseline_window_emits_no_evidence(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T09:00:00+08:00,bank-api,cpu_usage,10\n"
        "2026-07-14T12:03:00+08:00,bank-api,cpu_usage,95\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.evidence_items == []


def test_log_provider_filters_keyword_level_instance_and_limit(fixture_root: Path):
    provider = OpenRcaLogProvider(fixture_root, runtime_case("Bank"))
    query = LogQuery(
        start_time=datetime(2026, 7, 14, 11, 55, tzinfo=TZ),
        end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
        reason="inspect checkout timeout",
        limit=1,
        keywords=["timeout"],
        levels=["ERROR"],
        instance="bank-api-2",
    )

    result = provider.collect(event("Bank"), query)

    matches = result.evidence_items[0].payload["matches"]
    assert len(matches) == 1
    assert matches[0]["instance"] == "bank-api-2"
    assert "timeout" in matches[0]["message"].lower()
    assert "root_cause_claims" not in result.evidence_items[0].payload


def test_log_provider_namespaces_evidence_id_per_investigation(fixture_root: Path):
    case = runtime_case("Bank")
    first = OpenRcaLogProvider(fixture_root, case, evidence_namespace="inv-fixed").collect(
        event("Bank")
    )
    second = OpenRcaLogProvider(fixture_root, case, evidence_namespace="inv-adaptive").collect(
        event("Bank")
    )

    assert first.evidence_items[0].payload == second.evidence_items[0].payload
    assert first.evidence_items[0].id != second.evidence_items[0].id


def test_dependency_provider_aggregates_parent_child_latency(fixture_root: Path):
    provider = OpenRcaDependencyProvider(fixture_root, runtime_case("Market/cloudbed-1"))
    query = DependencyQuery(
        start_time=datetime(2026, 7, 14, 11, 55, tzinfo=TZ),
        end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
        reason="inspect checkout dependencies",
        target="checkoutservice",
    )

    result = provider.collect(event("Market/cloudbed-1"), query)

    edge = result.evidence_items[0].payload["edges"][0]
    assert set(edge) >= {"parent", "child", "latency", "error_count"}
    assert edge["parent"] == "checkoutservice"
    assert edge["child"] == "paymentservice"
    assert result.evidence_items[0].payload["dependency"] == "paymentservice"
    assert "root_cause_claims" not in result.evidence_items[0].payload


def test_dependency_provider_does_not_emit_normal_edge(tmp_path: Path):
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:05:00+08:00", 100, 200),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert result.evidence_items == []


def test_dependency_provider_projects_segment_quality_fields(tmp_path: Path):
    # F18：dependency latency segment 与 metric adapter 同一模式投影 additive
    # 质量字段，供 basis-aware 排序读取。
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:01:00+08:00", 100, 200),
            ("2026-07-14T12:05:00+08:00", 500, 200),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert len(result.evidence_items) == 1
    payload = result.evidence_items[0].payload
    assert payload["strength_basis"] == "relative"
    assert payload["anomaly_point_count"] == 1


def test_dependency_provider_degenerate_baseline_projects_presence_only_range(
    tmp_path: Path,
):
    # F18：退化（零）dependency baseline 的 segment 投影 presence-only 值域；
    # 不得以缺字段的 legacy 身份参与跨 basis 数值比较（违反 F6 语义）。
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:30:00+08:00", 0, 200),
            ("2026-07-14T11:40:00+08:00", 0, 200),
            ("2026-07-14T11:50:00+08:00", 0, 200),
            ("2026-07-14T12:01:00+08:00", 100, 200),
            ("2026-07-14T12:03:00+08:00", 200, 200),
            ("2026-07-14T12:05:00+08:00", 300, 200),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert len(result.evidence_items) == 1
    payload = result.evidence_items[0].payload
    assert payload["strength_basis"] == "presence_only"
    assert payload["normalized_strength"] == 1.0
    assert payload["deviation_score"] == 1.0
    assert payload["anomaly_point_count"] == 3


def test_dependency_provider_uses_segment_onset_for_latency(tmp_path: Path):
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:01:00+08:00", 100, 200),
            ("2026-07-14T12:05:00+08:00", 500, 200),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert item.timestamp == datetime(2026, 7, 14, 12, 5, tzinfo=TZ)
    assert item.payload["signal_type"] == "latency"
    assert item.payload["baseline_value"] == 100
    assert item.payload["normalized_strength"] == 10
    assert item.payload["anomaly_onset"] == item.timestamp.isoformat()
    assert item.payload["anomaly_segment_id"]


def test_dependency_provider_emits_error_evidence_with_first_error_onset(tmp_path: Path):
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:01:00+08:00", 100, 500),
            ("2026-07-14T12:03:00+08:00", 100, 500),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert item.timestamp == datetime(2026, 7, 14, 12, 1, tzinfo=TZ)
    assert item.payload["signal_type"] == "timeout"
    assert item.payload["edges"][0]["error_count"] == 2
    assert item.payload["baseline_value"] == 100


def test_dependency_provider_separates_latency_segment_from_errors(tmp_path: Path):
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:03:00+08:00", 100, 500),
            ("2026-07-14T12:05:00+08:00", 500, 200),
        ],
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    by_type = {item.payload["signal_type"]: item for item in result.evidence_items}
    assert set(by_type) == {"latency", "timeout"}
    assert by_type["timeout"].timestamp == datetime(2026, 7, 14, 12, 3, tzinfo=TZ)
    assert by_type["latency"].timestamp == datetime(2026, 7, 14, 12, 5, tzinfo=TZ)


def test_dependency_provider_does_not_emit_self_edge(tmp_path: Path):
    dataset = dependency_dataset(
        tmp_path,
        [
            ("2026-07-14T11:20:00+08:00", 90, 200),
            ("2026-07-14T11:30:00+08:00", 100, 200),
            ("2026-07-14T11:40:00+08:00", 110, 200),
            ("2026-07-14T12:05:00+08:00", 500, 200),
        ],
        child_component="checkoutservice",
    )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), None
    )

    assert result.evidence_items == []


def test_dependency_provider_ignores_valid_spans_outside_query_window(
    fixture_root: Path, tmp_path: Path
):
    dataset = tmp_path / "dataset"
    shutil.copytree(fixture_root / "Market", dataset / "Market")
    traces = dataset / "Market" / "cloudbed-1" / "telemetry" / "2026-07-14" / "traces.csv"
    with traces.open("a", encoding="utf-8") as file:
        file.write(
            "trace-out,span-out-parent,,checkoutservice,"
            "2026-07-14T10:00:00+08:00,100,200\n"
            "trace-out,span-out-child,span-out-parent,paymentservice,"
            "2026-07-14T10:00:01+08:00,200,200\n"
        )

    result = OpenRcaDependencyProvider(dataset, runtime_case("Market/cloudbed-1")).collect(
        event("Market/cloudbed-1"), None
    )

    assert result.status == ProviderStatus.SUCCESS


def test_provider_rejects_telemetry_path_escape(fixture_root: Path):
    case = runtime_case("Bank").model_copy(update={"telemetry_dir": "../../record.csv"})

    with pytest.raises(ValueError, match="outside dataset root"):
        OpenRcaLogProvider(fixture_root, case)


def test_metric_provider_keeps_valid_rows_and_reports_malformed_rows(
    fixture_root: Path, tmp_path: Path
):
    dataset = tmp_path / "dataset"
    shutil.copytree(fixture_root / "Bank", dataset / "Bank")
    metrics = dataset / "Bank" / "telemetry" / "2026-07-14" / "metrics.csv"
    with metrics.open("a", encoding="utf-8") as file:
        file.write("not-a-time,bank-api,cpu_usage,not-a-number,bank-api-1\n")

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.status == ProviderStatus.PARTIAL
    assert result.evidence_items[0].payload["anomaly_segment_id"]
    assert "malformed" in (result.error_message or "")


def test_metric_provider_reads_official_nested_metric_directory(tmp_path: Path):
    directory = tmp_path / "Bank" / "telemetry" / "2026_07_14" / "metric"
    directory.mkdir(parents=True)
    (directory / "kpi.csv").write_text(
        "timestamp,cmdb_id,kpi_name,value\n"
        + baseline_csv("bank-api", "cpu", 10)
        + "2026-07-14T12:05:00+08:00,bank-api,cpu,95\n",
        encoding="utf-8",
    )
    case = runtime_case("Bank").model_copy(update={"telemetry_dir": "Bank/telemetry/2026_07_14"})

    result = OpenRcaMetricProvider(tmp_path, case).collect(event("Bank"), metric_query())

    assert result.evidence_items[0].payload["anomaly_segment_id"]


def test_metric_provider_recognizes_bank_tc_component(tmp_path: Path):
    directory = tmp_path / "Bank" / "telemetry" / "2026_07_14" / "metric"
    directory.mkdir(parents=True)
    (directory / "metric_app.csv").write_text(
        "timestamp,rr,tc\n"
        "2026-07-14T11:42:00+08:00,10,ServiceTest1\n"
        "2026-07-14T11:47:00+08:00,11,ServiceTest1\n"
        "2026-07-14T11:52:00+08:00,9,ServiceTest1\n"
        "2026-07-14T12:05:00+08:00,95,ServiceTest1\n",
        encoding="utf-8",
    )
    case = runtime_case("Bank").model_copy(update={"telemetry_dir": "Bank/telemetry/2026_07_14"})

    result = OpenRcaMetricProvider(tmp_path, case).collect(event("Bank"), metric_query())

    assert result.evidence_items[0].payload["component"] == "ServiceTest1"


# --- counter-like normalization（V10.1 T3） ----------------------------------


def _series_points(*rows: tuple[str, float]) -> list[SeriesPoint]:
    return [
        SeriesPoint(timestamp=datetime.fromisoformat(timestamp), value=value)
        for timestamp, value in rows
    ]


@pytest.mark.parametrize(
    "metric_name",
    [
        "total_requests",
        "http_requests_total",
        "request_count",
        "event_counter",
        "HTTP_ERRORS_TOTAL",
    ],
)
def test_counter_like_name_recognizes_cumulative_naming(metric_name: str):
    assert _is_counter_like_name(metric_name)


@pytest.mark.parametrize(
    "metric_name",
    [
        "memory_usage",
        "cpu_usage",
        "request_duration",
        "total",
        "recount",
        "discount",
        "counterfeit",
    ],
)
def test_counter_like_name_rejects_gauge_naming(metric_name: str):
    assert not _is_counter_like_name(metric_name)


def test_normalize_counter_like_emits_right_timestamp_per_second_rate():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", 120),
        ("2026-07-14T11:45:00+08:00", 240),
    )

    rates = _normalize_counter_like("http_requests_total", points)

    assert [(point.timestamp.isoformat(), point.value) for point in rates] == [
        ("2026-07-14T11:43:00+08:00", 2.0),
        ("2026-07-14T11:45:00+08:00", 1.0),
    ]


def test_normalize_counter_like_sorts_unordered_input_before_evaluating():
    ordered = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", 120),
        ("2026-07-14T11:45:00+08:00", 240),
    )
    shuffled = [ordered[2], ordered[0], ordered[1]]

    assert _normalize_counter_like(
        "http_requests_total", shuffled
    ) == _normalize_counter_like("http_requests_total", ordered)


def test_normalize_counter_like_keeps_gauge_for_unmatched_name():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 10),
        ("2026-07-14T11:43:00+08:00", 20),
        ("2026-07-14T11:44:00+08:00", 30),
    )

    assert _normalize_counter_like("memory_usage", points) == points


def test_normalize_counter_like_rejects_constant_series():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 5),
        ("2026-07-14T11:43:00+08:00", 5),
        ("2026-07-14T11:44:00+08:00", 5),
        ("2026-07-14T11:45:00+08:00", 5),
    )

    assert _normalize_counter_like("total_requests", points) == points


def test_normalize_counter_like_requires_two_positive_deltas():
    # 仅一个正向 delta 不足以确认累计语义，保持原 gauge 路径。
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", 0),
        ("2026-07-14T11:44:00+08:00", 5),
        ("2026-07-14T11:45:00+08:00", 5),
    )

    assert _normalize_counter_like("total_requests", points) == points


def test_normalize_counter_like_rejects_reset():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 100),
        ("2026-07-14T11:43:00+08:00", 200),
        ("2026-07-14T11:44:00+08:00", 50),
    )

    assert _normalize_counter_like("http_requests_total", points) == points


def test_normalize_counter_like_rejects_duplicate_timestamps_after_sorting():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:42:00+08:00", 5),
        ("2026-07-14T11:43:00+08:00", 10),
    )

    assert _normalize_counter_like("http_requests_total", points) == points


def test_normalize_counter_like_rejects_non_finite_values():
    infinite = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", float("inf")),
        ("2026-07-14T11:44:00+08:00", 10),
    )
    nan_points = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", float("nan")),
        ("2026-07-14T11:44:00+08:00", 10),
    )

    assert _normalize_counter_like("http_requests_total", infinite) == infinite
    assert _normalize_counter_like("http_requests_total", nan_points) == nan_points


def test_normalize_counter_like_requires_three_points():
    points = _series_points(
        ("2026-07-14T11:42:00+08:00", 0),
        ("2026-07-14T11:43:00+08:00", 10),
    )

    assert _normalize_counter_like("total_requests", points) == points


def test_metric_provider_constant_rate_counter_produces_no_anomaly(tmp_path: Path):
    # 等速累计 counter 的绝对水平持续升高，但 rate 恒定，不得产生异常。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T11:42:00+08:00,bank-api,http_requests_total,0\n"
        "2026-07-14T11:47:00+08:00,bank-api,http_requests_total,300\n"
        "2026-07-14T11:52:00+08:00,bank-api,http_requests_total,600\n"
        "2026-07-14T11:57:00+08:00,bank-api,http_requests_total,900\n"
        "2026-07-14T12:02:00+08:00,bank-api,http_requests_total,1200\n"
        "2026-07-14T12:07:00+08:00,bank-api,http_requests_total,1500\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert result.evidence_items == []


def test_metric_provider_counter_rate_acceleration_uses_right_side_onset(tmp_path: Path):
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T11:42:00+08:00,bank-api,http_requests_total,0\n"
        "2026-07-14T11:47:00+08:00,bank-api,http_requests_total,300\n"
        "2026-07-14T11:52:00+08:00,bank-api,http_requests_total,600\n"
        "2026-07-14T11:57:00+08:00,bank-api,http_requests_total,900\n"
        "2026-07-14T12:02:00+08:00,bank-api,http_requests_total,1200\n"
        "2026-07-14T12:07:00+08:00,bank-api,http_requests_total,6000\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    # onset 取加速区间的右侧样本 timestamp；强度来自 rate 而非原始累计值。
    assert item.timestamp == datetime(2026, 7, 14, 12, 7, tzinfo=TZ)
    assert item.payload["anomaly_onset"] == item.timestamp.isoformat()
    assert item.payload["baseline_value"] == 1.0
    assert item.payload["current_value"] == 16.0
    assert item.payload["normalized_strength"] == 10


def test_metric_provider_monotonic_memory_gauge_stays_gauge(tmp_path: Path):
    # 命名不符合累计语义的单调增长 gauge（如 memory）不得被误转为 rate（spec F3）。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T11:42:00+08:00,bank-api,memory_usage,10\n"
        "2026-07-14T11:47:00+08:00,bank-api,memory_usage,20\n"
        "2026-07-14T11:52:00+08:00,bank-api,memory_usage,30\n"
        "2026-07-14T12:03:00+08:00,bank-api,memory_usage,85\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert item.payload["current_value"] == 85
    assert item.payload["baseline_value"] == 20


def test_metric_provider_counter_reset_keeps_gauge_semantics(tmp_path: Path):
    # 窗口内出现 reset 的 counter 命名 series 不转换，完整保留 gauge 行为。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T11:42:00+08:00,bank-api,http_requests_total,100\n"
        "2026-07-14T11:47:00+08:00,bank-api,http_requests_total,200\n"
        "2026-07-14T11:52:00+08:00,bank-api,http_requests_total,50\n"
        "2026-07-14T12:03:00+08:00,bank-api,http_requests_total,700\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert item.payload["current_value"] == 700


def test_metric_provider_out_of_window_reset_does_not_block_counter_conversion(
    tmp_path: Path,
):
    # F10 合同：09:00 的窗口外高值相对窗口内起点构成 reset；先截取窗口再
    # normalize，窗口内合法 counter 仍转换为 rate。
    rows = (
        "timestamp,cmdb_id,kpi_name,value\n"
        "2026-07-14T09:00:00+08:00,bank-api,http_requests_total,100000\n"
        "2026-07-14T11:42:00+08:00,bank-api,http_requests_total,100\n"
        "2026-07-14T11:47:00+08:00,bank-api,http_requests_total,400\n"
        "2026-07-14T11:52:00+08:00,bank-api,http_requests_total,700\n"
        "2026-07-14T12:03:00+08:00,bank-api,http_requests_total,7900\n"
    )
    dataset = metric_dataset(tmp_path, rows)

    result = OpenRcaMetricProvider(dataset, runtime_case("Bank")).collect(
        event("Bank"), metric_query()
    )

    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert item.timestamp == datetime(2026, 7, 14, 12, 3, tzinfo=TZ)
    # current_value 是 per-second rate 而非原始累计值，证明窗口内转换生效。
    assert item.payload["current_value"] == pytest.approx(7200 / 660)
