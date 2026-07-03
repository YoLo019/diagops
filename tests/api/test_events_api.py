from fastapi.testclient import TestClient

from backend.main import app


def test_create_simulated_event_returns_completed_investigation():
    client = TestClient(app)

    response = client.post("/events/simulated/deployment_regression")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert isinstance(body["status"], str)
    assert body["service"] == "payment-service"
    assert body["top_cause_type"] == "deployment_regression"


def test_create_webhook_event_returns_completed_investigation():
    client = TestClient(app)

    response = client.post(
        "/events",
        json={
            "source": "webhook",
            "service": "checkout-service",
            "environment": "prod",
            "severity": "warning",
            "title": "Latency increased",
            "description": "checkout-service latency increased during QPS spike",
            "started_at": "2026-07-03T15:10:00+08:00",
            "time_window_minutes": 30,
            "signals": {"qps": "high", "latency": "high"},
        },
    )

    assert response.status_code == 200
    assert response.json()["top_cause_type"] == "traffic_spike"


def test_unknown_simulated_case_returns_404():
    client = TestClient(app)

    response = client.post("/events/simulated/not_a_case")

    assert response.status_code == 404
