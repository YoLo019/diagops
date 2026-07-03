from fastapi.testclient import TestClient

from backend.main import app


def test_get_investigation_after_simulated_event():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    response = client.get(f"/investigations/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["status"] == "completed"
    assert isinstance(body["status"], str)
    assert body["report"]["markdown"].startswith("# payment-service RCA")


def test_list_investigations_includes_created_record():
    client = TestClient(app)
    created = client.post("/events/simulated/dependency_timeout").json()

    response = client.get("/investigations")

    assert response.status_code == 200
    assert any(item["id"] == created["id"] for item in response.json())


def test_unknown_investigation_returns_404():
    client = TestClient(app)

    response = client.get("/investigations/inv-not-found")

    assert response.status_code == 404
