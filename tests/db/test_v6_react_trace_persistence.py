from datetime import UTC, datetime

from sqlalchemy import insert, select

from backend.db.models import InvestigationRecord
from backend.db.schema import react_traces
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)
from backend.services.incident_cases import load_incident_case


def dt(minutes: int) -> datetime:
    return datetime(2026, 7, 8, 9, minutes, tzinfo=UTC)


def trace() -> ReActTrace:
    return ReActTrace(
        id="react-legacy",
        investigation_id="inv-legacy",
        status=ReActTraceStatus.COMPLETED,
        final_answer="historical answer",
        steps=[
            ReActTraceStep(
                step_number=1,
                assistant_text="Check read-only logs.",
                tool_name="read_logs",
                tool_input={"service": "checkout"},
                observation="Errors rose after deploy.",
                status=ReActTraceStepStatus.OBSERVED,
                started_at=dt(1),
                completed_at=dt(2),
            )
        ],
        created_at=dt(0),
        completed_at=dt(3),
    )


def test_sqlite_reads_directly_inserted_historical_trace_without_writer(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'legacy-react.db'}"
    engine = create_db_engine(database_url)
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(
        InvestigationRecord(
            id="inv-legacy",
            event=load_incident_case("deployment_regression"),
        )
    )
    legacy = trace()
    payload = legacy.model_dump(mode="json")
    with engine.begin() as connection:
        connection.execute(
            insert(react_traces).values(
                id=legacy.id,
                investigation_id=legacy.investigation_id,
                payload=payload,
                created_at=payload["created_at"],
            )
        )
    engine.dispose()

    restarted_engine = create_db_engine(database_url)
    initialize_database(restarted_engine)
    restarted = SQLiteInvestigationRepository(restarted_engine)
    assert restarted.get_react_trace("inv-legacy") == legacy

    record = restarted.get("inv-legacy")
    record.event = record.event.model_copy(update={"title": "later aggregate save"})
    restarted.save(record)
    with restarted_engine.connect() as connection:
        stored_payload = connection.execute(
            select(react_traces.c.payload).where(
                react_traces.c.investigation_id == "inv-legacy"
            )
        ).scalar_one()

    assert stored_payload == payload
    assert not hasattr(restarted, "save_react_trace")
