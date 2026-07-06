from fastapi.testclient import TestClient

from backend.config.settings import AppSettings, StorageSettings
from backend.main import app
from backend.services.container import reset_container


def sqlite_settings(tmp_path) -> AppSettings:
    return AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'diagops-api.db'}")
    )


def test_sqlite_api_persists_investigation_across_container_instances(tmp_path):
    settings = sqlite_settings(tmp_path)
    reset_container(settings=settings)
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    reset_container(settings=settings)
    fresh_client = TestClient(app)

    list_response = fresh_client.get("/investigations")
    detail_response = fresh_client.get(f"/investigations/{created['id']}")

    assert list_response.status_code == 200
    assert any(item["id"] == created["id"] for item in list_response.json())
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == created["id"]


def test_sqlite_api_persists_action_and_verification_status_updates(tmp_path):
    settings = sqlite_settings(tmp_path)
    reset_container(settings=settings)
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    action_id = detail["actions"][0]["id"]
    verification_id = detail["verification_suggestions"][0]["id"]

    action_response = client.patch(
        f"/investigations/{created['id']}/actions/{action_id}",
        json={"status": "approved", "note": "owner approved"},
    )
    verification_response = client.patch(
        f"/investigations/{created['id']}/verifications/{verification_id}",
        json={"status": "passed", "result_note": "5xx recovered"},
    )
    reset_container(settings=settings)
    fresh_client = TestClient(app)

    stored = fresh_client.get(f"/investigations/{created['id']}").json()

    assert action_response.status_code == 200
    assert verification_response.status_code == 200
    assert stored["actions"][0]["status"] == "approved"
    assert stored["actions"][0]["note"] == "owner approved"
    assert stored["verification_suggestions"][0]["status"] == "passed"
    assert stored["verification_suggestions"][0]["result_note"] == "5xx recovered"
