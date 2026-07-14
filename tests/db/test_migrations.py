from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.db.migrations import CURRENT_SCHEMA_VERSION, SchemaCompatibilityError
from backend.db.schema import (
    agent_executions,
    context_facts,
    diagnosis_plans,
    diagnosis_tasks,
    events,
    evidence_items,
    hypotheses,
    investigations,
    llm_analyses,
    memory_items,
    provider_results,
    recommended_actions,
    reports,
    schema_version,
    specialist_results,
    tool_calls,
    verification_suggestions,
)
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.events import IncidentEvent, IncidentSource, Severity

_V3_TABLES = (
    schema_version,
    investigations,
    events,
    evidence_items,
    hypotheses,
    recommended_actions,
    verification_suggestions,
    reports,
    provider_results,
    specialist_results,
    llm_analyses,
)
_V4_TABLES = (
    diagnosis_plans,
    diagnosis_tasks,
    agent_executions,
    context_facts,
    tool_calls,
    memory_items,
)


@pytest.mark.parametrize("historical_version", [3, 4])
def test_historical_schema_migrates_to_v5_and_preserves_rows(
    tmp_path, historical_version: int
) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / f'v{historical_version}.db'}")
    _create_historical_schema(engine, historical_version)
    _insert_historical_record(engine, historical_version)

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        versions = connection.execute(select(schema_version.c.version)).scalars().all()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        llm_payload = connection.execute(select(llm_analyses.c.payload)).scalar_one()
    assert versions == [CURRENT_SCHEMA_VERSION]
    assert foreign_keys == 1
    assert llm_payload["summary"] == "historical analysis"
    assert SQLiteInvestigationRepository(engine).get("inv-history").event.title == "history"


def test_v4_schema_with_single_current_marker_migrates_to_v5(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v4-single-marker.db'}")
    _create_historical_schema(engine, 4)
    with engine.begin() as connection:
        connection.execute(delete(schema_version))
        connection.execute(insert(schema_version).values(version=4))
    _insert_historical_record(engine, 4)

    initialize_database(engine)

    with engine.connect() as connection:
        versions = connection.execute(select(schema_version.c.version)).scalars().all()
    restored = SQLiteInvestigationRepository(engine).get("inv-history")
    assert versions == [CURRENT_SCHEMA_VERSION]
    assert restored.event.title == "history"
    assert restored.llm_analysis is not None


@pytest.mark.parametrize("database_url", ["sqlite:///:memory:", "file"])
def test_fresh_database_reaches_v5_and_enforces_foreign_keys(
    tmp_path, database_url: str
) -> None:
    if database_url == "file":
        database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    engine = create_db_engine(database_url)

    initialize_database(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.execute(select(schema_version.c.version)).scalars().all() == [5]
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                insert(events).values(
                    investigation_id="inv-missing",
                    payload={"source": "webhook"},
                )
            )


@pytest.mark.parametrize("claimed_versions", [(3,), (4,), (3, 4)])
def test_corrupt_claimed_schema_fails_without_modification(
    tmp_path, claimed_versions: tuple[int, ...]
) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'corrupt.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE investigations (id VARCHAR PRIMARY KEY)")
        for version in claimed_versions:
            connection.exec_driver_sql(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    before_tables = set(inspect(engine).get_table_names())

    with pytest.raises(SchemaCompatibilityError):
        initialize_database(engine)

    assert set(inspect(engine).get_table_names()) == before_tables
    with engine.connect() as connection:
        stored_versions = tuple(
            connection.exec_driver_sql("SELECT version FROM schema_version").scalars()
        )
        assert stored_versions == claimed_versions


def test_foreign_keys_reject_parent_delete(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'foreign-key.db'}")
    initialize_database(engine)
    _insert_current_parent_and_event(engine)

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                investigations.delete().where(investigations.c.id == "inv-history")
            )


def _create_historical_schema(engine, version: int) -> None:
    with engine.begin() as connection:
        for table in _V3_TABLES:
            table.create(connection)
        if version == 4:
            for table in _V4_TABLES:
                table.create(connection)
        versions = [3] if version == 3 else [3, 4]
        connection.execute(insert(schema_version), [{"version": item} for item in versions])


def _insert_historical_record(engine, version: int) -> None:
    event = _event().model_dump(mode="json")
    now = datetime(2026, 7, 13, tzinfo=UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            insert(investigations).values(
                id="inv-history",
                event=event,
                status="completed",
                failure_reason=None,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        connection.execute(insert(events).values(investigation_id="inv-history", payload=event))
        connection.execute(
            insert(llm_analyses).values(
                investigation_id="inv-history",
                payload={
                    "investigation_id": "inv-history",
                    "summary": "historical analysis",
                    "missing_evidence": [],
                    "risk_notes": [],
                    "suggested_questions": [],
                    "referenced_evidence_ids": [],
                    "created_at": now,
                },
            )
        )
        if version == 4:
            connection.execute(
                insert(diagnosis_plans).values(
                    id="plan-history",
                    investigation_id="inv-history",
                    created_at=now,
                    payload={
                        "id": "plan-history",
                        "investigation_id": "inv-history",
                        "tasks": [],
                        "created_at": now,
                    },
                )
            )


def _insert_current_parent_and_event(engine) -> None:
    event = _event().model_dump(mode="json")
    now = datetime(2026, 7, 13, tzinfo=UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            insert(investigations).values(
                id="inv-history",
                event=event,
                status="completed",
                failure_reason=None,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        connection.execute(insert(events).values(investigation_id="inv-history", payload=event))


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.WEBHOOK,
        service="checkout-service",
        environment="prod",
        severity=Severity.WARNING,
        title="history",
        description="historical record",
        started_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
