import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.container import get_container, reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


def test_post_feedback_returns_memory_and_persists_without_changing_report():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    before = client.get(f"/investigations/{created['id']}").json()

    response = client.post(
        f"/investigations/{created['id']}/feedback",
        json={
            "root_cause_correct": True,
            "action_useful": False,
            "verification_result": "passed",
            "note": "rollback fixed the error rate",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["memory_type"] == "human_feedback"
    assert body["service"] == before["event"]["service"]
    assert body["environment"] == before["event"]["environment"]
    assert body["source_investigation_id"] == created["id"]

    memory_items = get_container().repository.list_memory(
        before["event"]["service"],
        before["event"]["environment"],
    )
    assert [item.id for item in memory_items] == [body["id"]]

    after = client.get(f"/investigations/{created['id']}").json()
    assert after["report"] == before["report"]
    assert after["hypotheses"] == before["hypotheses"]
    assert after["actions"] == before["actions"]
    assert after["verification_suggestions"] == before["verification_suggestions"]


def test_post_feedback_unknown_investigation_returns_404():
    client = TestClient(app)

    response = client.post("/investigations/inv-missing/feedback", json={})

    assert response.status_code == 404
