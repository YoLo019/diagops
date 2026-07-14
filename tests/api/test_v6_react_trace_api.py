from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.config.settings import AppSettings, StorageSettings
from backend.db.schema import llm_analyses, react_traces
from backend.domain.llm_analysis import LLMAnalysis
from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)
from backend.main import app
from backend.services.container import get_container, reset_container


def test_react_trace_api_reads_historical_sqlite_row_after_restart(tmp_path):
    settings = AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'legacy-api.db'}")
    )
    reset_container(settings)
    client = TestClient(app)
    investigation_id = client.post(
        "/events/simulated/deployment_regression"
    ).json()["id"]
    trace = ReActTrace(
        id="react-legacy",
        investigation_id=investigation_id,
        status=ReActTraceStatus.COMPLETED,
        final_answer="Bearer historical-trace-token",
        steps=[
            ReActTraceStep(
                step_number=1,
                assistant_text="Bearer historical-step-token",
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
    payload = trace.model_dump(mode="json")
    analysis = LLMAnalysis.create(
        investigation_id=investigation_id,
        existing_evidence_ids=set(),
        summary="Historical LLM analysis.",
    )
    with get_container().engine.begin() as connection:
        connection.execute(
            insert(react_traces).values(
                id=trace.id,
                investigation_id=investigation_id,
                payload=payload,
                created_at=payload["created_at"],
            )
        )
        connection.execute(
            insert(llm_analyses).values(
                investigation_id=investigation_id,
                payload=analysis.model_dump(mode="json"),
            )
        )

    reset_container(settings)
    response = client.get(f"/investigations/{investigation_id}/react-trace")

    assert response.status_code == 200
    assert response.json()["id"] == "react-legacy"
    assert "historical-trace-token" not in response.text
    assert "historical-step-token" not in response.text
    assert "[REDACTED]" in response.text
    detail = client.get(f"/investigations/{investigation_id}")
    assert detail.status_code == 200
    assert detail.json()["llm_analysis"]["summary"] == "Historical LLM analysis."


def test_unknown_investigation_react_trace_endpoint_returns_404():
    reset_container()
    response = TestClient(app).get("/investigations/inv-not-found/react-trace")

    assert response.status_code == 404
