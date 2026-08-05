import logging
from datetime import UTC, datetime, timedelta

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.domain.tool_queries import (
    DependencyQuery,
    DeploymentQuery,
    LogQuery,
    MetricQuery,
    ServiceCatalogQuery,
)
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider
from backend.providers.mock_service_catalog import MockServiceCatalogProvider
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def test_mock_registry_collects_deployment_regression_evidence():
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    evidence = registry.collect_all(event)

    summaries = [item.summary for item in evidence]
    assert any("NullPointerException" in summary for summary in summaries)
    assert any("v1.8.2" in summary for summary in summaries)
    assert any("QPS stayed within normal range" in summary for summary in summaries)


def test_mock_registry_collects_dependency_timeout_evidence():
    event = load_incident_case("dependency_timeout")
    registry = build_mock_provider_registry()

    evidence = registry.collect_all(event)

    assert any("inventory-service latency increased" in item.summary for item in evidence)


def test_mock_registry_collects_provider_results():
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    results = registry.collect_results(event)

    assert {result.provider for result in results} >= {
        EvidenceProvider.LOG,
        EvidenceProvider.METRIC,
        EvidenceProvider.DEPLOY,
        EvidenceProvider.DEPENDENCY,
        EvidenceProvider.SERVICE_CATALOG,
        EvidenceProvider.RELATED_ALERT,
    }
    assert all(result.status == "success" for result in results)


def test_registry_converts_provider_failure_to_error_evidence():
    class FailingProvider:
        provider = EvidenceProvider.LOG

        def collect(self, event):
            raise RuntimeError("provider exploded")

    event = load_incident_case("deployment_regression")
    registry = ProviderRegistry([FailingProvider()])

    evidence = registry.collect_all(event)

    failed = next(item for item in evidence if item.provider == EvidenceProvider.LOG)
    assert failed.kind == EvidenceKind.PROVIDER_ERROR
    assert failed.status == EvidenceStatus.FAILED
    assert failed.error_message == "provider collection failed"
    assert sum(item.status == EvidenceStatus.SKIPPED for item in evidence) == 5


def test_provider_registry_logs_provider_status(caplog):
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    with caplog.at_level(logging.INFO):
        registry.collect_results(event)

    messages = "\n".join(item.message for item in caplog.records)
    assert "provider completed" in messages
    assert "provider=" in messages
    assert "status=success" in messages
    assert "duration_ms" in messages
    assert "evidence_count=" in messages


def test_simulation_provider_is_not_called_for_manual_event() -> None:
    class RecordingProvider:
        provider = EvidenceProvider.LOG

        def __init__(self) -> None:
            self.calls = 0

        def collect(self, event: IncidentEvent) -> ProviderResult:
            self.calls += 1
            return ProviderResult(provider=self.provider)

    recording = RecordingProvider()
    registry = ProviderRegistry(providers=[], simulation_providers=[recording])
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.WARNING,
        title="manual incident",
        description="no configured real provider",
        started_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    results = registry.collect_results(event)

    assert recording.calls == 0
    assert {result.provider for result in results} == {
        EvidenceProvider.LOG,
        EvidenceProvider.METRIC,
        EvidenceProvider.DEPLOY,
        EvidenceProvider.DEPENDENCY,
        EvidenceProvider.SERVICE_CATALOG,
        EvidenceProvider.RELATED_ALERT,
    }
    assert all(result.status == ProviderStatus.SKIPPED for result in results)
    hypotheses = RcaAnalyzer().analyze(event, registry.evidence_from_results(results))
    assert hypotheses[0].cause_type == "unknown"


def test_mock_queries_filter_metric_deployment_catalog_and_dependency_results():
    event = load_incident_case("deployment_regression")
    window = {
        "start_time": event.started_at - timedelta(minutes=5),
        "end_time": event.started_at + timedelta(minutes=5),
        "reason": "验证参数过滤",
    }

    metrics = MockMetricProvider().collect(
        event, MetricQuery(**window, metric_names=["error_rate"])
    ).evidence_items
    deployments = MockDeployProvider().collect(
        event, DeploymentQuery(**window, version="missing")
    ).evidence_items
    catalog = MockServiceCatalogProvider().collect(
        event, ServiceCatalogQuery(**window, include_dependencies=False)
    ).evidence_items[0]
    dependencies = MockDependencyProvider().collect(
        event, DependencyQuery(**window, target="missing-service")
    ).evidence_items

    assert metrics and set(metrics[0].payload) == {"error_rate"}
    assert deployments == []
    assert catalog.payload["dependencies"] == []
    assert dependencies == []


def test_mock_log_query_honors_level_filter():
    event = load_incident_case("deployment_regression")
    window = {
        "start_time": event.started_at - timedelta(minutes=5),
        "end_time": event.started_at + timedelta(minutes=5),
        "reason": "验证日志级别",
    }

    info = MockLogProvider().collect(event, LogQuery(**window, levels=["INFO"]))
    error = MockLogProvider().collect(event, LogQuery(**window, levels=["ERROR"]))

    assert info.evidence_items == []
    assert len(error.evidence_items) == 1
