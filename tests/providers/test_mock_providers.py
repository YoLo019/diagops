from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
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

    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.PROVIDER_ERROR
    assert evidence[0].status == EvidenceStatus.FAILED
    assert evidence[0].error_message == "provider exploded"
