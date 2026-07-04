from backend.providers.registry import build_mock_provider_registry
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
