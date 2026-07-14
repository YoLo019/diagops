import logging
from datetime import UTC, datetime

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
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
    assert {result.provider for result in results} == set(EvidenceProvider)
    assert all(result.status == ProviderStatus.SKIPPED for result in results)
    hypotheses = RcaAnalyzer().analyze(event, registry.evidence_from_results(results))
    assert hypotheses[0].cause_type == "unknown"
