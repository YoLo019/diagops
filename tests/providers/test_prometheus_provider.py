import json
import math
import urllib.parse
from datetime import UTC, datetime, timedelta

from backend.config.settings import (
    AppSettings,
    DeploymentFileProviderSettings,
    LogFileProviderSettings,
    PrometheusProviderSettings,
    ProviderSettings,
    ProviderToggle,
    ServiceCatalogProviderSettings,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.domain.tool_queries import MetricAggregation, PrometheusMetric, PrometheusQuery
from backend.providers.prometheus import PrometheusProvider
from backend.providers.registry import ProviderRegistry, build_provider_registry_from_settings
from backend.providers.results import ProviderStatus

# _event() 窗口：观测 [07:30, 08:30]，等长前置 baseline [06:30, 07:30]。
BASE_TS = datetime(2026, 7, 6, 6, 30, tzinfo=UTC).timestamp()
OBSERVATION_TS = datetime(2026, 7, 6, 7, 30, tzinfo=UTC).timestamp()

# 各 metric 模板的识别子串；顺序保证 5xx 先于裸 http_requests_total 命中。
_NEEDLES = (
    ("5..", "5xx_rate"),
    ("duration_seconds_bucket", "p95_latency"),
    ("cpu_usage", "cpu"),
    ("memory_usage", "memory"),
    ("packets_dropped", "network_drops"),
    ("restarts", "process_restarts"),
    ("http_requests_total", "qps"),
)


def _series(baseline: float = 10.0, anomaly: float | None = 50.0) -> list[list]:
    points = [[BASE_TS + 300 * index, str(baseline)] for index in range(12)]
    for index in range(12):
        value = baseline if anomaly is None or index < 3 else anomaly
        points.append([OBSERVATION_TS + 300 * index, str(value)])
    return points


def _matrix_body(points: list[list]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": ([{"metric": {}, "values": points}] if points else []),
        },
    }


def _vector_body(value: float) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [OBSERVATION_TS, str(value)]}],
        },
    }


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self) -> int:
        return 200

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


def _needle_for(query: str) -> str | None:
    for needle, _name in _NEEDLES:
        if needle in query:
            return needle
    return None


def _mock_urlopen(
    *,
    anomalous: frozenset[str] = frozenset(name for _n, name in _NEEDLES),
    empty: frozenset[str] = frozenset(),
    range_failures: frozenset[str] = frozenset(),
    instant_values: dict[str, float] | None = None,
    instant_failures: frozenset[str] = frozenset(),
    range_points: dict[str, list[list]] | None = None,
    recorded: list | None = None,
):
    """按 needle 路由 range/instant 响应；recorded 非空时记录所有请求。"""

    def _urlopen(request, timeout):
        url = request.full_url
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        query = params["query"][0]
        if recorded is not None:
            recorded.append((url, params))
        needle = _needle_for(query)
        name = dict(_NEEDLES).get(needle)
        if "/api/v1/query_range" in url:
            if needle in range_failures:
                raise OSError(f"{name} range boom")
            if range_points and needle in range_points:
                return _FakeResponse(_matrix_body(range_points[needle]))
            if needle in empty:
                return _FakeResponse(_matrix_body([]))
            if name in anomalous:
                return _FakeResponse(_matrix_body(_series()))
            return _FakeResponse(_matrix_body(_series(anomaly=None)))
        if needle in instant_failures:
            raise OSError(f"{name} instant boom")
        values = instant_values or {}
        if needle in values:
            return _FakeResponse(_vector_body(values[needle]))
        raise AssertionError(f"unexpected instant query: {query}")

    return _urlopen


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="checkout-service has elevated errors",
        started_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        time_window_minutes=30,
    )


def test_disabled_prometheus_provider_is_not_added_by_settings_builder():
    registry = build_provider_registry_from_settings(AppSettings())

    assert not any(isinstance(provider, PrometheusProvider) for provider in registry.providers)


def test_range_anomalies_create_segment_evidence_for_all_signals(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen())

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.METRIC
    assert {item.payload["signal_name"] for item in result.evidence_items} == {
        "qps",
        "5xx_rate",
        "p95_latency",
        "cpu",
        "memory",
        "network_drops",
        "process_restarts",
    }
    for item in result.evidence_items:
        assert item.kind == EvidenceKind.METRIC_TREND
        assert item.payload["anomaly_onset"] == item.timestamp.isoformat()
        assert item.payload["anomaly_segment_id"]
        assert item.payload["normalized_strength"] <= 10
        assert item.payload["deviation_score"] == item.payload["normalized_strength"]
        assert item.payload["baseline_value"] == 10.0
        assert item.payload["component"] == "checkout-service"
        assert "base_url" not in item.payload


def test_optional_signal_types_map_to_network_corruption_and_process(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen())

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    by_name = {item.payload["signal_name"]: item for item in result.evidence_items}
    assert by_name["network_drops"].payload["signal_type"] == "network_corruption"
    assert by_name["process_restarts"].payload["signal_type"] == "process"
    assert by_name["qps"].payload["signal_type"] == "traffic"


def test_flat_core_and_empty_optional_series_are_not_failures(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            anomalous=frozenset(),
            empty=frozenset({"packets_dropped", "restarts"}),
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []
    assert result.error_message is None


def test_empty_core_series_falls_back_to_instant_with_onset_unavailable(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            empty=frozenset({"http_requests_total", "packets_dropped", "restarts"}),
            instant_values={"http_requests_total": 123.4},
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    instant = next(
        item for item in result.evidence_items if item.payload["signal_name"] == "qps"
    )
    assert instant.payload["onset_unavailable"] is True
    assert instant.payload["query_names"] == ["qps"]
    assert instant.payload["observed_values"] == {"qps": 123.4}
    assert instant.payload["baseline_values"] == {"qps": 123.4}
    assert instant.payload["change_percent"] == 0.0
    assert instant.payload["service"] == "checkout-service"


def test_range_failure_falls_back_to_instant_with_onset_unavailable(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            range_failures=frozenset({"memory_usage"}),
            empty=frozenset({"packets_dropped", "restarts"}),
            instant_values={"memory_usage": 2048.0},
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.PARTIAL
    assert "memory range" in (result.error_message or "")
    memory = next(
        item for item in result.evidence_items if item.payload["signal_name"] == "memory"
    )
    assert memory.payload["onset_unavailable"] is True
    assert memory.status == EvidenceStatus.PARTIAL


def test_range_and_instant_failure_drops_metric_visibly(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            range_failures=frozenset({"memory_usage"}),
            instant_failures=frozenset({"memory_usage"}),
            empty=frozenset({"packets_dropped", "restarts"}),
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.PARTIAL
    assert "memory" not in {item.payload["signal_name"] for item in result.evidence_items}
    assert "memory range" in (result.error_message or "")
    assert "memory current" in (result.error_message or "")


def test_range_non_finite_point_is_explicit_failure(monkeypatch):
    points = _series()
    points[15] = [points[15][0], "NaN"]
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            range_points={"memory_usage": points},
            empty=frozenset({"packets_dropped", "restarts"}),
            instant_values={"memory_usage": 2048.0},
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.PARTIAL
    memory = next(
        item for item in result.evidence_items if item.payload["signal_name"] == "memory"
    )
    assert memory.payload["onset_unavailable"] is True


def test_range_over_point_limit_is_explicit_failure(monkeypatch):
    points = [[BASE_TS + 30 * index, "10"] for index in range(241)]
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen(
            range_points={"memory_usage": points},
            empty=frozenset({"packets_dropped", "restarts"}),
            instant_values={"memory_usage": 2048.0},
        ),
    )

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.PARTIAL
    assert "memory range" in (result.error_message or "")


def test_range_step_bounds_points_per_series(monkeypatch):
    recorded: list = []
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen(recorded=recorded))
    event = _event()
    query = PrometheusQuery(
        start_time=event.started_at,
        end_time=event.started_at + timedelta(hours=2),
        reason="验证步长上限",
        metric_names=[PrometheusMetric.CPU],
    )

    PrometheusProvider("http://prometheus.test").collect(event, query)

    range_params = next(
        params for url, params in recorded if "/api/v1/query_range" in url
    )
    # baseline + observation 共 4 小时，step 保证每 series 约不超过 240 点。
    assert int(range_params["step"][0]) >= math.ceil(4 * 3600 / 240)


def test_query_requests_only_selected_templates(monkeypatch):
    recorded: list = []
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen(recorded=recorded))
    event = _event()
    query = PrometheusQuery(
        start_time=event.started_at,
        end_time=event.started_at + timedelta(minutes=10),
        reason="验证资源饱和",
        metric_names=[PrometheusMetric.CPU, PrometheusMetric.MEMORY],
        aggregation=MetricAggregation.MAX,
    )

    result = PrometheusProvider("http://prometheus.test").collect(event, query)

    assert {item.payload["signal_name"] for item in result.evidence_items} == {
        "cpu",
        "memory",
    }
    queries = [params["query"][0] for _url, params in recorded]
    assert len(queries) == 2
    assert all(item.startswith("max(") for item in queries)


def test_optional_templates_convert_counters_with_rate(monkeypatch):
    recorded: list = []
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen(recorded=recorded))

    PrometheusProvider("http://prometheus.test").collect(_event())

    queries = [params["query"][0] for _url, params in recorded]
    assert any("rate(container_network_receive_packets_dropped_total" in q for q in queries)
    assert any("rate(kube_pod_container_status_restarts_total" in q for q in queries)


def test_connection_failure_returns_failed_provider_result_through_registry(monkeypatch):
    def fail_urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    registry = ProviderRegistry([PrometheusProvider("http://prometheus.test")])

    results = registry.collect_results(_event())
    evidence = registry.collect_all(_event())

    assert results[0].provider == EvidenceProvider.METRIC
    assert results[0].status == ProviderStatus.FAILED
    assert "connection refused" in (results[0].error_message or "")
    failed = next(item for item in evidence if item.provider == EvidenceProvider.METRIC)
    assert failed.kind == EvidenceKind.PROVIDER_ERROR
    assert failed.status == EvidenceStatus.FAILED
    assert "connection refused" in (failed.error_message or "")


def test_enabled_prometheus_provider_is_added_by_settings_builder(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _mock_urlopen())
    settings = AppSettings(
        providers=ProviderSettings(
            mock=ProviderToggle(enabled=False),
            log_file=LogFileProviderSettings(enabled=False),
            deployment_file=DeploymentFileProviderSettings(enabled=False),
            service_catalog=ServiceCatalogProviderSettings(enabled=False),
            prometheus=PrometheusProviderSettings(
                enabled=True,
                base_url="http://prometheus.test",
            ),
        )
    )

    registry = build_provider_registry_from_settings(settings)
    results = registry.collect_results(_event())

    assert any(isinstance(provider, PrometheusProvider) for provider in registry.providers)
    metric_result = next(
        result for result in results if result.provider == EvidenceProvider.METRIC
    )
    assert "base_url" not in metric_result.evidence_items[0].payload
    assert sum(result.status == ProviderStatus.SKIPPED for result in results) == 5
