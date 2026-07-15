import pytest
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


def test_webhook_without_real_metric_provider_preserves_unknown_cause():
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
    assert response.json()["status"] == "completed"
    assert response.json()["top_cause_type"] == "unknown"


def test_raw_event_accepts_adaptive_strategy_query():
    client = TestClient(app)
    response = client.post(
        "/events?strategy=adaptive",
        json={
            "source": "webhook",
            "service": "checkout-service",
            "environment": "prod",
            "severity": "warning",
            "title": "Latency increased",
            "description": "checkout-service latency increased",
            "started_at": "2026-07-03T15:10:00+08:00",
        },
    )

    assert response.status_code == 200
    detail = client.get(f"/investigations/{response.json()['id']}").json()
    assert detail["strategy"] == "adaptive"


def test_create_event_summary_includes_v2_counts():
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
    body = response.json()
    assert body["status"] == "completed"
    assert body["action_count"] >= 1
    assert body["verification_count"] >= 1


def test_manual_investigation_requires_service_and_environment():
    client = TestClient(app)

    response = client.post(
        "/investigations/manual",
        json={"text": "checkout-service has many 500s"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["text", "service", "environment"])
def test_manual_investigation_rejects_blank_fields(field: str):
    client = TestClient(app)
    payload = {
        "text": "checkout-service has many 500s",
        "service": "checkout-service",
        "environment": "prod",
    }
    payload[field] = "   "

    response = client.post(
        "/investigations/manual",
        json=payload,
    )

    assert response.status_code == 422


def test_manual_investigation_creates_completed_record():
    client = TestClient(app)

    response = client.post(
        "/investigations/manual",
        json={
            "text": "checkout-service has many 500s after 14:00",
            "service": "checkout-service",
            "environment": "prod",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "checkout-service"
    assert body["status"] == "completed"


def test_manual_investigation_accepts_adaptive_strategy():
    client = TestClient(app)
    response = client.post(
        "/investigations/manual",
        json={
            "text": "checkout-service has many 500s",
            "service": "checkout-service",
            "environment": "prod",
            "strategy": "adaptive",
        },
    )

    assert response.status_code == 200
    detail = client.get(f"/investigations/{response.json()['id']}").json()
    assert detail["strategy"] == "adaptive"


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


def test_to_summary_handles_record_without_hypotheses():
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
        failure_reason="provider timeout",
    )

    summary = events.to_summary(record)

    assert summary.top_cause_type == "unknown"
    assert summary.confidence == 0.0
    assert summary.failure_reason == "provider timeout"
