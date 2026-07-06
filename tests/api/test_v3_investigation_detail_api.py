import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.container import reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def investigation_id(client: TestClient) -> str:
    created = client.post("/events/simulated/deployment_regression")
    assert created.status_code == 200
    return created.json()["id"]


def test_investigation_detail_endpoints_return_expanded_record_data(
    client: TestClient, investigation_id: str
):
    timeline_response = client.get(f"/investigations/{investigation_id}/timeline")
    evidence_response = client.get(f"/investigations/{investigation_id}/evidence")
    provider_results_response = client.get(
        f"/investigations/{investigation_id}/provider-results"
    )
    specialist_results_response = client.get(
        f"/investigations/{investigation_id}/specialist-results"
    )
    report_response = client.get(f"/investigations/{investigation_id}/report")

    assert timeline_response.status_code == 200
    assert evidence_response.status_code == 200
    assert provider_results_response.status_code == 200
    assert specialist_results_response.status_code == 200
    assert report_response.status_code == 200

    timeline = timeline_response.json()
    evidence = evidence_response.json()
    assert timeline
    assert evidence
    assert [item["timestamp"] for item in timeline] == sorted(
        item["timestamp"] for item in timeline
    )
    assert len(provider_results_response.json()) > 0
    assert len(specialist_results_response.json()) > 0

    report = report_response.json()
    assert report["investigation_id"] == investigation_id
    assert report["markdown"].startswith("# payment-service RCA")


def test_investigation_evidence_provider_filter_returns_only_matching_provider(
    client: TestClient, investigation_id: str
):
    response = client.get(f"/investigations/{investigation_id}/evidence?provider=log")

    assert response.status_code == 200
    evidence = response.json()
    assert evidence
    assert {item["provider"] for item in evidence} == {"log"}


@pytest.mark.parametrize(
    "path",
    [
        "/investigations/inv-not-found/timeline",
        "/investigations/inv-not-found/evidence",
        "/investigations/inv-not-found/provider-results",
        "/investigations/inv-not-found/specialist-results",
        "/investigations/inv-not-found/report",
    ],
)
def test_unknown_investigation_detail_endpoints_return_404(client: TestClient, path: str):
    response = client.get(path)

    assert response.status_code == 404


def test_config_providers_returns_enabled_state_and_safe_config(client: TestClient):
    response = client.get("/config/providers")

    assert response.status_code == 200
    providers = {provider["name"]: provider for provider in response.json()["providers"]}
    assert set(providers) == {
        "mock",
        "log_file",
        "prometheus",
        "deployment_file",
        "service_catalog",
    }

    assert providers["mock"] == {"name": "mock", "enabled": True, "config": {}}
    assert providers["log_file"]["enabled"] is True
    assert providers["log_file"]["config"] == {
        "paths": ["data/sample-logs/checkout-service.log"]
    }
    assert providers["prometheus"]["enabled"] is False
    assert providers["prometheus"]["config"] == {"base_url": "http://127.0.0.1:9090"}
    assert providers["deployment_file"]["enabled"] is True
    assert providers["deployment_file"]["config"] == {
        "path": "data/deployments/deployments.json"
    }
    assert providers["service_catalog"]["enabled"] is True
    assert providers["service_catalog"]["config"] == {"path": "config/services.yaml"}
