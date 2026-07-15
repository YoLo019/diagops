import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.benchmarks.openrca.models import OpenRcaPartition, OpenRcaRuntimeCase
from backend.benchmarks.openrca.providers import (
    OpenRcaDependencyProvider,
    OpenRcaLogProvider,
    OpenRcaMetricProvider,
)
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


def metric_query() -> MetricQuery:
    return MetricQuery(
        start_time=datetime(2026, 7, 14, 11, 55, tzinfo=TZ),
        end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
        reason="inspect anomalous metrics",
        limit=10,
        metric_names=[],
    )


@pytest.mark.parametrize("partition", ["Bank", "Telecom", "Market/cloudbed-1"])
def test_metric_provider_normalizes_schema_variants(
    partition: str, fixture_root: Path
):
    provider = OpenRcaMetricProvider(fixture_root, runtime_case(partition))

    result = provider.collect(event(partition), metric_query())

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items[0].kind == EvidenceKind.METRIC_TREND
    assert result.evidence_items[0].payload["anomalies"]
    assert "telemetry" not in str(result.evidence_items[0].payload).lower()


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


def test_dependency_provider_aggregates_parent_child_latency(fixture_root: Path):
    provider = OpenRcaDependencyProvider(
        fixture_root, runtime_case("Market/cloudbed-1")
    )
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


def test_provider_rejects_telemetry_path_escape(fixture_root: Path):
    case = runtime_case("Bank").model_copy(
        update={"telemetry_dir": "../../record.csv"}
    )

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
    assert result.evidence_items[0].payload["anomalies"]
    assert "malformed" in (result.error_message or "")
