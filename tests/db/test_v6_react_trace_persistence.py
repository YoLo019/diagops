from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, select

from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import metadata, react_traces
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)


def build_sqlite_repository():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return SQLiteInvestigationRepository(engine), engine


def dt(minutes: int) -> datetime:
    return datetime(2026, 7, 8, 9, minutes, tzinfo=UTC)


def trace(trace_id: str, answer: str) -> ReActTrace:
    return ReActTrace(
        id=trace_id,
        investigation_id="inv-1",
        status=ReActTraceStatus.COMPLETED,
        final_answer=answer,
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


def test_in_memory_repository_saves_round_trips_and_replaces_react_trace():
    repository = InMemoryInvestigationRepository()
    first = trace("react-1", "first")
    replacement = trace("react-2", "second")

    assert repository.get_react_trace("missing") is None
    assert repository.save_react_trace(first) == first
    repository.save_react_trace(replacement)

    assert repository.get_react_trace("inv-1") == replacement


def test_sqlite_repository_saves_round_trips_and_replaces_react_trace():
    repository, engine = build_sqlite_repository()
    first = trace("react-1", "first")
    replacement = trace("react-2", "second")

    columns = {
        column["name"]: column for column in inspect(engine).get_columns("react_traces")
    }

    assert "react_traces" in inspect(engine).get_table_names()
    assert columns["investigation_id"]["nullable"] is False
    assert columns["payload"]["nullable"] is False
    assert columns["created_at"]["nullable"] is False
    assert repository.get_react_trace("missing") is None

    assert repository.save_react_trace(first) == first
    repository.save_react_trace(replacement)

    with engine.connect() as connection:
        raw_created_at = connection.execute(
            select(react_traces.c.created_at).where(
                react_traces.c.investigation_id == "inv-1"
            )
        ).scalar_one()

    assert isinstance(raw_created_at, str)
    assert "2026-07-08T09:00:00" in raw_created_at
    assert repository.get_react_trace("inv-1") == replacement
