from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)
from backend.main import app
from backend.services.container import get_container, reset_container


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


def test_react_trace_endpoint_returns_null_when_missing(
    client: TestClient,
    investigation_id: str,
):
    response = client.get(f"/investigations/{investigation_id}/react-trace")

    assert response.status_code == 200
    assert response.json() is None


def test_react_trace_endpoint_returns_persisted_trace(
    client: TestClient,
    investigation_id: str,
):
    get_container().repository.save_react_trace(
        ReActTrace(
            id="react-1",
            investigation_id=investigation_id,
            status=ReActTraceStatus.COMPLETED,
            final_answer="done",
            steps=[
                ReActTraceStep(
                    step_number=1,
                    assistant_text="Check read-only logs.",
                    tool_name="read_logs",
                    tool_input={"service": "checkout"},
                    observation="Errors rose after deploy.",
                    output_evidence_ids=["ev-1"],
                    status=ReActTraceStepStatus.OBSERVED,
                    started_at=datetime(2026, 7, 8, 9, 1, tzinfo=UTC),
                    completed_at=datetime(2026, 7, 8, 9, 2, tzinfo=UTC),
                )
            ],
            created_at=datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 8, 9, 3, tzinfo=UTC),
        )
    )

    response = client.get(f"/investigations/{investigation_id}/react-trace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "react-1"
    assert payload["investigation_id"] == investigation_id
    assert payload["status"] == "completed"
    assert payload["final_answer"] == "done"
    assert payload["steps"][0]["tool_name"] == "read_logs"
    assert payload["steps"][0]["output_evidence_ids"] == ["ev-1"]


def test_unknown_investigation_react_trace_endpoint_returns_404(client: TestClient):
    response = client.get("/investigations/inv-not-found/react-trace")

    assert response.status_code == 404
