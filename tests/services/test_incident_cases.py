from backend.domain.events import IncidentSource
from backend.services.incident_cases import list_case_ids, load_incident_case


def test_list_case_ids_returns_all_seed_cases():
    assert list_case_ids() == [
        "database_slowdown",
        "dependency_timeout",
        "deployment_regression",
        "single_bad_instance",
        "traffic_spike",
    ]


def test_load_incident_case_returns_incident_event():
    event = load_incident_case("deployment_regression")

    assert event.source == IncidentSource.SIMULATED
    assert event.service == "payment-service"
    assert event.signals["error_rate"] == "high"
