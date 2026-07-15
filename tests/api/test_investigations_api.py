import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.container import reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


def test_list_investigations_starts_empty():
    client = TestClient(app)

    response = client.get("/investigations")

    assert response.status_code == 200
    assert response.json() == []


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


def test_list_investigation_summaries_is_newest_first_and_excludes_details():
    client = TestClient(app)
    older = client.post("/events/simulated/deployment_regression").json()
    newer = client.post("/events/simulated/dependency_timeout").json()

    response = client.get("/investigations/summaries")

    assert response.status_code == 200
    summaries = response.json()
    assert [item["id"] for item in summaries] == [newer["id"], older["id"]]
    assert set(summaries[0]) == {
        "id",
        "status",
        "service",
        "title",
        "strategy",
        "top_cause_type",
        "confidence",
        "action_count",
        "verification_count",
        "failure_reason",
    }
    assert client.get("/investigations/summaries/evidence").status_code == 404


def test_unknown_investigation_returns_404():
    client = TestClient(app)

    response = client.get("/investigations/inv-not-found")

    assert response.status_code == 404


def test_update_action_status_only_changes_state():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    action_id = detail["actions"][0]["id"]

    response = client.patch(
        f"/investigations/{created['id']}/actions/{action_id}",
        json={"status": "approved", "note": "owner approved"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["note"] == "owner approved"


def test_update_unknown_action_returns_404():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    response = client.patch(
        f"/investigations/{created['id']}/actions/act-not-found",
        json={"status": "approved"},
    )

    assert response.status_code == 404


def test_update_verification_status_records_result_note():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    verification_id = detail["verification_suggestions"][0]["id"]
    evidence_id = detail["evidence"][0]["id"]
    action_id = detail["actions"][0]["id"]

    response = client.patch(
        f"/investigations/{created['id']}/verifications/{verification_id}",
        json={
            "status": "passed",
            "result_note": "5xx recovered",
            "result_evidence_ids": [evidence_id],
            "related_action_ids": [action_id],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["result_note"] == "5xx recovered"


def test_direct_high_risk_done_and_empty_verification_result_return_conflict():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    high_risk = next(action for action in detail["actions"] if action["requires_approval"])
    verification = detail["verification_suggestions"][0]

    action_response = client.patch(
        f"/investigations/{created['id']}/actions/{high_risk['id']}",
        json={"status": "done"},
    )
    verification_response = client.patch(
        f"/investigations/{created['id']}/verifications/{verification['id']}",
        json={"status": "passed", "result_note": " "},
    )

    assert action_response.status_code == 409
    assert action_response.json()["code"] == "action_transition"
    assert verification_response.status_code == 409
    assert verification_response.json()["code"] == "verification_result_note_required"
