import json
import urllib.parse
from datetime import UTC, datetime

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
    assert evidence.payload["base_url"] == "http://prometheus.test"
    assert set(evidence.payload["queries"]) == {
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
    assert results[0].error_message == "connection refused"
    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.PROVIDER_ERROR
    assert evidence[0].provider == EvidenceProvider.METRIC
    assert evidence[0].status == EvidenceStatus.FAILED
    assert evidence[0].error_message == "connection refused"


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
    assert len(results) == 1
    assert results[0].evidence_items[0].payload["base_url"] == "http://prometheus.test"


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
