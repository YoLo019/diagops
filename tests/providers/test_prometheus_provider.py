import json
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


def test_disabled_prometheus_provider_is_not_added_by_settings_builder():
    registry = build_provider_registry_from_settings(AppSettings())

    assert not any(isinstance(provider, PrometheusProvider) for provider in registry.providers)


def test_successful_mocked_http_responses_create_metric_trend_evidence(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_success)
    provider = PrometheusProvider("http://prometheus.test")

    result = provider.collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.METRIC
    assert len(result.evidence_items) == 1
    evidence = result.evidence_items[0]
    assert evidence.kind == EvidenceKind.METRIC_TREND
    assert evidence.payload["query_names"] == [
        "qps",
        "5xx_rate",
        "p95_latency",
        "cpu",
        "memory",
    ]


def test_evidence_payload_contains_query_names_and_observed_values(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_success)
    evidence = PrometheusProvider("http://prometheus.test").collect(_event()).evidence_items[0]

    assert evidence.payload["service"] == "checkout-service"
    assert evidence.payload["environment"] == "prod"
    assert "base_url" not in evidence.payload
    assert "queries" not in evidence.payload
    assert set(evidence.payload["query_names"]) == {
        "qps",
        "5xx_rate",
        "p95_latency",
        "cpu",
        "memory",
    }
    assert evidence.payload["observed_values"] == {
        "qps": 123.4,
        "5xx_rate": 3.0,
        "p95_latency": 0.42,
        "cpu": 0.91,
        "memory": 2048.0,
    }


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
    monkeypatch.setattr("urllib.request.urlopen", _urlopen_success)
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


def test_prometheus_partial_response_retains_valid_observations(monkeypatch):
    seen_times: list[str] = []

    def partial_urlopen(request, timeout):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        seen_times.extend(params["time"])
        query = params["query"][0]
        if "container_memory_usage_bytes" in query:
            raise TimeoutError("memory query timed out")
        return _urlopen_success(request, timeout)

    monkeypatch.setattr("urllib.request.urlopen", partial_urlopen)

    result = PrometheusProvider("http://prometheus.test").collect(_event())

    assert result.status == ProviderStatus.PARTIAL
    assert len(result.evidence_items) == 1
    assert "memory" not in result.evidence_items[0].payload["observed_values"]
    assert len(seen_times) == 5
    assert set(seen_times) == {str(_event().started_at.timestamp())}


def test_prometheus_query_requests_only_selected_templates(monkeypatch):
    seen: list[tuple[str, str]] = []

    def recording_urlopen(request, timeout):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        seen.append((params["query"][0], params["time"][0]))
        return _urlopen_success(request, timeout)

    monkeypatch.setattr("urllib.request.urlopen", recording_urlopen)
    event = _event()
    query = PrometheusQuery(
        start_time=event.started_at,
        end_time=event.started_at + timedelta(minutes=10),
        reason="验证资源饱和",
        metric_names=[PrometheusMetric.CPU, PrometheusMetric.MEMORY],
        aggregation=MetricAggregation.MAX,
    )

    result = PrometheusProvider("http://prometheus.test").collect(event, query)

    assert result.evidence_items[0].payload["query_names"] == ["cpu", "memory"]
    assert len(seen) == 2
    assert all(item[0].startswith("max(") for item in seen)
    assert {item[1] for item in seen} == {str(query.end_time.timestamp())}


class _FakeResponse:
    def __init__(self, value: float) -> None:
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self) -> int:
        return 200

    def read(self) -> bytes:
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1783339200.0, str(self.value)]}],
                },
            }
        ).encode("utf-8")


def _urlopen_success(request, timeout):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["query"][0]
    values = {
        'status=~"5.."': 3.0,
        "http_requests_total": 123.4,
        "http_request_duration_seconds_bucket": 0.42,
        "container_cpu_usage_seconds_total": 0.91,
        "container_memory_usage_bytes": 2048.0,
    }
    for needle, value in values.items():
        if needle in query:
            return _FakeResponse(value)
    raise AssertionError(f"unexpected query: {query}")


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
