import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api import events
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.events import IncidentEvent
from backend.main import app
from backend.services.container import reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


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


def test_known_simulated_case_load_failure_returns_500(monkeypatch):
    client = TestClient(app)

    def fail_to_load(case_id: str) -> IncidentEvent:
        raise ValueError(f"Broken fixture: {case_id}")

    monkeypatch.setattr(events, "load_incident_case", fail_to_load)

    response = client.post("/events/simulated/deployment_regression")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to load simulated incident case"}


def test_to_summary_rejects_record_without_hypotheses():
    event = IncidentEvent(
        source="webhook",
        service="checkout-service",
        environment="prod",
        severity="warning",
        title="Latency increased",
        description="checkout-service latency increased",
        started_at="2026-07-03T15:10:00+08:00",
    )
    record = InvestigationRecord(
        event=event,
        status=InvestigationStatus.COMPLETED,
        hypotheses=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        events.to_summary(record)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Investigation has no hypotheses"
