from backend.diagnosis.context import SpecialistStatus
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.domain.evidence import EvidenceKind, EvidenceProvider
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.services.incident_cases import load_incident_case


def test_coordinator_collects_specialist_results_and_evidence():
    event = load_incident_case("deployment_regression")
    coordinator = DiagnosisCoordinator(build_mock_provider_registry())

    result = coordinator.collect(event)

    assert result.event == event
    assert result.evidence
    assert result.provider_results
    assert result.specialist_results
    assert all(item.status == SpecialistStatus.COMPLETED for item in result.specialist_results)
    assert {item.agent_name for item in result.specialist_results} >= {
        "LogAnalyst",
        "MetricAnalyst",
        "DeployAnalyst",
        "DependencyAnalyst",
        "ServiceCatalogAnalyst",
        "RelatedAlertAnalyst",
    }


def test_coordinator_records_failed_provider_as_failed_specialist():
    class FailingProvider:
        provider = "log"

        def collect(self, event):
            raise RuntimeError("provider exploded")

    event = load_incident_case("deployment_regression")
    coordinator = DiagnosisCoordinator(
        ProviderRegistry(providers=[], simulation_providers=[FailingProvider()])
    )

    result = coordinator.collect(event)

    provider_error = next(
        item
        for item in result.evidence
        if item.provider == EvidenceProvider.LOG
        and item.kind == EvidenceKind.PROVIDER_ERROR
    )
    log_specialist = next(
        item for item in result.specialist_results if item.agent_name == "LogAnalyst"
    )
    assert provider_error.error_message == "provider collection failed"
    assert log_specialist.status == SpecialistStatus.FAILED
    assert log_specialist.errors == ["provider collection failed"]
