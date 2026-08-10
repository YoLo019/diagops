import pytest
from fastapi.testclient import TestClient

from backend.api import events
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.events import IncidentEvent
from backend.main import app
from backend.services.container import get_container, reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


def test_create_simulated_event_returns_completed_investigation():
    with TestClient(app) as client:
        response = client.post("/events/simulated/deployment_regression")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert isinstance(body["status"], str)
    assert body["service"] == "payment-service"
    assert body["top_cause_type"] == "deployment_regression"
    assert body["runtime_available"] is True


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


@pytest.mark.parametrize(
    "environment",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "secret=ordinary-secret",
        "prod-secret",
        "prod-token",
        "prod-key",
        "https://operator:ordinary-secret@example.invalid/prod",
    ],
)
def test_raw_event_rejects_credential_like_environment(environment: str) -> None:
    response = TestClient(app).post(
        "/events",
        json={
            "source": "webhook",
            "service": "checkout-service",
            "environment": environment,
            "severity": "warning",
            "title": "Latency increased",
            "description": "checkout-service latency increased",
            "started_at": "2026-07-03T15:10:00+08:00",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("environment", ["secretary-prod", "tokenizer-prod"])
def test_raw_event_accepts_non_credential_environment_words(environment: str) -> None:
    response = TestClient(app).post(
        "/events",
        json={
            "source": "webhook",
            "service": "checkout-service",
            "environment": environment,
            "severity": "warning",
            "title": "Latency increased",
            "description": "checkout-service latency increased",
            "started_at": "2026-07-03T15:10:00+08:00",
        },
    )

    assert response.status_code == 200


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


@pytest.mark.parametrize(
    "environment",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "token=ordinary-secret",
        "https://operator:ordinary-secret@example.invalid/prod",
    ],
)
def test_manual_investigation_rejects_credential_like_environment(
    environment: str,
) -> None:
    response = TestClient(app).post(
        "/investigations/manual",
        json={
            "text": "checkout-service has many 500s",
            "service": "checkout-service",
            "environment": environment,
        },
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


def _alertmanager_alert(**updates):
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "ServiceDegraded",
            "service": "checkout-service",
            "environment": "prod",
            "severity": "warning",
            "scenario": "must-not-propagate",
        },
        "annotations": {
            "summary": "Service degradation detected",
            "description": "Latency and errors increased",
            "unknown": "must-not-propagate",
        },
        "startsAt": "2026-07-03T15:10:00+08:00",
        "generatorURL": "https://prometheus.invalid/secret",
    }
    alert.update(updates)
    return alert


def test_alertmanager_firing_creates_sanitized_investigation():
    response = TestClient(app).post(
        "/events/alertmanager",
        json={
            "externalURL": "https://alertmanager.invalid/secret",
            "alerts": [_alertmanager_alert()],
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "created"
    assert response.json()[0]["failure_category"] is None
    record = get_container().repository.list()[0]
    assert record.event.source == "webhook"
    assert record.event.service == "checkout-service"
    assert record.event.environment == "prod"
    assert record.event.severity == "warning"
    assert record.event.title == "Service degradation detected"
    assert record.event.description == "Latency and errors increased"
    assert record.event.started_at.utcoffset() is not None
    assert record.event.time_window_minutes == 30
    assert record.event.signals == {}
    serialized = record.model_dump_json() + response.text
    assert "must-not-propagate" not in serialized
    assert "prometheus.invalid" not in serialized
    assert "alertmanager.invalid" not in serialized


def test_alertmanager_resolved_is_ignored_before_creation_fields():
    response = TestClient(app).post(
        "/events/alertmanager",
        json={"alerts": [{"status": "resolved"}]},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "status": "ignored",
            "alert_index": 0,
            "investigation": None,
            "failure_category": None,
            "detail": None,
        }
    ]
    assert get_container().repository.list() == []


def test_alertmanager_mixed_batch_preserves_order_and_isolates_invalid_alert():
    response = TestClient(app).post(
        "/events/alertmanager",
        json={
            "alerts": [
                {"status": "resolved"},
                _alertmanager_alert(labels={"environment": "prod"}),
                _alertmanager_alert(),
            ]
        },
    )

    assert response.status_code == 200
    assert [item["status"] for item in response.json()] == [
        "ignored",
        "failed",
        "created",
    ]
    assert response.json()[1]["failure_category"] == "invalid_alert"
    assert response.json()[1]["detail"] == "Alert payload is invalid"
    assert len(get_container().repository.list()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"alerts": {}},
        {"alerts": [{"status": "resolved"}] * 101},
    ],
)
def test_alertmanager_rejects_invalid_envelope_atomically(payload):
    response = TestClient(app).post("/events/alertmanager", json=payload)

    assert response.status_code == 422
    assert get_container().repository.list() == []


@pytest.mark.parametrize(
    "update",
    [
        {"labels": {"service": "x" * 257, "environment": "prod"}},
        {
            "labels": {
                "service": "checkout-service",
                "environment": "secret=ordinary-secret",
            }
        },
        {"annotations": {"summary": "x" * 513}},
        {"annotations": {"summary": "valid", "description": "x" * 4097}},
        {"startsAt": "2026-07-03T15:10:00"},
    ],
)
def test_alertmanager_rejects_unsafe_or_out_of_bounds_alert(update):
    response = TestClient(app).post(
        "/events/alertmanager",
        json={"alerts": [_alertmanager_alert(**update)]},
    )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "failed"
    assert response.json()[0]["failure_category"] == "invalid_alert"
    assert response.json()[0]["detail"] == "Alert payload is invalid"
    assert get_container().repository.list() == []


def test_alertmanager_unknown_severity_safely_degrades_to_info():
    alert = _alertmanager_alert()
    alert["labels"]["severity"] = "page"

    response = TestClient(app).post(
        "/events/alertmanager",
        json={"alerts": [alert]},
    )

    assert response.status_code == 200
    assert get_container().repository.list()[0].event.severity == "info"


def test_alertmanager_duplicate_delivery_is_at_least_once():
    client = TestClient(app)
    payload = {"alerts": [_alertmanager_alert()]}

    first = client.post("/events/alertmanager", json=payload)
    second = client.post("/events/alertmanager", json=payload)

    assert first.status_code == second.status_code == 200
    assert len(get_container().repository.list()) == 2


def test_alertmanager_investigation_failure_does_not_block_later_alert(
    monkeypatch,
):
    container = get_container()
    original = container.run_investigation
    calls = 0

    async def fail_once(event, *, strategy=None, execution_contract_version=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unsafe provider detail")
        return await original(
            event,
            strategy=strategy,
            execution_contract_version=execution_contract_version,
        )

    monkeypatch.setattr(container, "run_investigation", fail_once)
    response = TestClient(app).post(
        "/events/alertmanager",
        json={"alerts": [_alertmanager_alert(), _alertmanager_alert()]},
    )

    assert response.status_code == 200
    assert [item["status"] for item in response.json()] == ["failed", "created"]
    assert response.json()[0]["failure_category"] == "investigation_failed"
    assert response.json()[0]["detail"] == "Investigation failed"
    assert "unsafe provider detail" not in response.text
    assert len(container.repository.list()) == 1
