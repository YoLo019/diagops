from fastapi.testclient import TestClient

from backend.config.settings import AppSettings, StorageSettings
from backend.db.sqlite_repository import SQLiteInvestigationRepository
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
    evidence_id = detail["evidence"][0]["id"]

    action_response = client.patch(
        f"/investigations/{created['id']}/actions/{action_id}",
        json={"status": "approved", "note": "owner approved"},
    )
    verification_response = client.patch(
        f"/investigations/{created['id']}/verifications/{verification_id}",
        json={
            "status": "passed",
            "result_note": "5xx recovered",
            "result_evidence_ids": [evidence_id],
            "related_action_ids": [action_id],
        },
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
    assert stored["verification_suggestions"][0]["result_evidence_ids"] == [
        evidence_id
    ]


def test_manual_secret_is_redacted_before_sqlite_and_api_projection(tmp_path):
    database_path = tmp_path / "diagops-api.db"
    settings = AppSettings(storage=StorageSettings(url=f"sqlite:///{database_path}"))
    reset_container(settings=settings)
    client = TestClient(app)

    created = client.post(
        "/investigations/manual",
        json={
            "text": "Bearer manual-secret owner@example.com /srv/private/log.txt",
            "service": "checkout-service",
            "environment": "prod",
        },
    )
    detail = client.get(f"/investigations/{created.json()['id']}")

    assert created.status_code == 200
    assert detail.status_code == 200
    assert "manual-secret" not in detail.text
    assert "owner@example.com" not in detail.text
    reset_container(settings=settings)
    assert b"manual-secret" not in database_path.read_bytes()


def test_in_memory_sqlite_is_shared_until_explicit_container_reset():
    settings = AppSettings(storage=StorageSettings(url="sqlite:///:memory:"))
    container = reset_container(settings=settings)
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    second_repository = SQLiteInvestigationRepository(container.engine)

    assert second_repository.get(created["id"]).id == created["id"]
    assert client.get(f"/investigations/{created['id']}").status_code == 200

    reset_container(settings=settings)
    assert TestClient(app).get("/investigations").json() == []
