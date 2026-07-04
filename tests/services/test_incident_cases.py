import pytest

from backend.domain.events import IncidentEvent, IncidentSource
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


def test_load_incident_cases_from_non_project_root_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert list_case_ids() == [
        "database_slowdown",
        "dependency_timeout",
        "deployment_regression",
        "single_bad_instance",
        "traffic_spike",
    ]
    assert load_incident_case("deployment_regression").service == "payment-service"


@pytest.mark.parametrize("case_id", list_case_ids())
def test_load_all_seed_cases(case_id):
    assert isinstance(load_incident_case(case_id), IncidentEvent)


def test_load_unknown_incident_case_raises_value_error():
    with pytest.raises(ValueError, match="Unknown incident case: missing_case"):
        load_incident_case("missing_case")


@pytest.mark.parametrize(
    "case_id",
    ["../deployment_regression", "folder/deployment_regression"],
)
def test_load_incident_case_rejects_path_segments(case_id):
    with pytest.raises(ValueError, match=f"Unknown incident case: {case_id}"):
        load_incident_case(case_id)
