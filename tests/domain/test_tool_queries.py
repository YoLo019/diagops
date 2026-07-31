from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.domain.tool_queries import (
    DependencyDirection,
    DependencyQuery,
    DeploymentQuery,
    LogQuery,
    MetricAggregation,
    MetricQuery,
    PrometheusMetric,
    PrometheusQuery,
    QueryWindow,
    ServiceCatalogQuery,
)


def _window(**overrides):
    start = datetime(2026, 7, 15, 8, tzinfo=UTC)
    return {
        "start_time": start,
        "end_time": start + timedelta(minutes=30),
        "reason": "确认故障时间窗口",
        **overrides,
    }


def test_query_contract_enum_values_are_stable():
    assert [item.value for item in MetricAggregation] == ["avg", "max", "sum"]
    assert [item.value for item in PrometheusMetric] == [
        "qps",
        "5xx_rate",
        "p95_latency",
        "cpu",
        "memory",
        "network_drops",
        "process_restarts",
    ]
    assert [item.value for item in DependencyDirection] == ["upstream", "downstream"]


def test_query_window_accepts_bounded_timezone_aware_range():
    window = QueryWindow(**_window())

    assert window.limit == 20
    assert window.end_time - window.start_time == timedelta(minutes=30)


@pytest.mark.parametrize(
    "changes",
    [
        {"start_time": datetime(2026, 7, 15, 8)},
        {"end_time": datetime(2026, 7, 15, 9)},
        {"end_time": datetime(2026, 7, 15, 8, tzinfo=UTC)},
        {"end_time": datetime(2026, 7, 15, 10, 0, 1, tzinfo=UTC)},
        {"reason": ""},
        {"reason": "x" * 241},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_query_window_rejects_invalid_boundaries(changes):
    start = changes.pop("start_time", datetime(2026, 7, 15, 8, tzinfo=UTC))
    values = {
        "start_time": start,
        "end_time": start + timedelta(minutes=30),
        "reason": "确认故障时间窗口",
        **changes,
    }

    with pytest.raises(ValidationError):
        QueryWindow(**values)


def test_specialist_queries_apply_defaults_and_strict_shapes():
    window = _window()
    log_query = LogQuery(**window, keywords=["timeout"], levels=["ERROR"])
    metric_query = MetricQuery(**window, metric_names=["latency"])
    prometheus_query = PrometheusQuery(
        **window, metric_names=[PrometheusMetric.P95_LATENCY]
    )

    assert log_query.instance is None
    assert metric_query.aggregation == MetricAggregation.AVG
    assert prometheus_query.aggregation == MetricAggregation.SUM
    assert DeploymentQuery(**window).version is None
    assert ServiceCatalogQuery(**window).include_dependencies is True
    assert DependencyQuery(**window, target="payment").direction == (
        DependencyDirection.DOWNSTREAM
    )
    assert DependencyQuery(**window, target="payment").depth == 1

    with pytest.raises(ValidationError):
        LogQuery(**window, keywords=[], unexpected=True)


def test_prometheus_query_accepts_all_supported_metrics_in_one_call():
    query = PrometheusQuery(**_window(), metric_names=list(PrometheusMetric))

    # 上限与 enum 基数保持同步：单次工具调用可覆盖全部受支持信号。
    assert len(query.metric_names) == 7


@pytest.mark.parametrize(
    "query_type, values",
    [
        (LogQuery, {"keywords": [str(index) for index in range(9)]}),
        (LogQuery, {"keywords": ["x" * 121]}),
        (LogQuery, {"keywords": ["error"], "levels": [str(index) for index in range(6)]}),
        (LogQuery, {"keywords": ["error"], "levels": ["x" * 33]}),
        (LogQuery, {"keywords": ["error"], "instance": "x" * 161}),
        (MetricQuery, {"metric_names": [str(index) for index in range(21)]}),
        (MetricQuery, {"metric_names": ["x" * 161]}),
        (PrometheusQuery, {"metric_names": []}),
        (PrometheusQuery, {"metric_names": ["qps"] * 8}),
        (DeploymentQuery, {"version": "x" * 121}),
        (DependencyQuery, {"target": "payment", "depth": 2}),
    ],
)
def test_specialist_queries_reject_out_of_contract_values(query_type, values):
    with pytest.raises(ValidationError):
        query_type(**_window(), **values)
