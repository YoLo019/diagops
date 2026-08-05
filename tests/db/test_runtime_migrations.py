from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.db.migrations import CURRENT_SCHEMA_VERSION
from backend.db.schema import (
    investigations,
    metadata,
    runtime_attempts,
    runtime_checkpoints,
    runtime_events,
    runtime_runs,
    schema_version,
)
from backend.db.session import create_db_engine, initialize_database
from backend.domain.events import IncidentEvent, IncidentSource, Severity

_RUNTIME_TABLE_NAMES = {
    "runtime_runs",
    "runtime_attempts",
    "runtime_events",
    "runtime_checkpoints",
}


def test_v5_database_migrates_to_v7_without_synthetic_runs(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v5.db'}")
    _create_v5_fixture(engine, investigation_id="inv-history")

    initialize_database(engine)

    with engine.connect() as connection:
        assert connection.execute(select(schema_version.c.version)).scalar_one() == 7
        assert connection.execute(select(runtime_runs)).all() == []
        assert connection.execute(select(investigations.c.id)).scalar_one() == "inv-history"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert CURRENT_SCHEMA_VERSION == 7
    assert _RUNTIME_TABLE_NAMES <= set(inspect(engine).get_table_names())
    assert "timeout_seconds" in {
        column["name"] for column in inspect(engine).get_columns("runtime_runs")
    }


def test_legacy_v6_runtime_row_gets_compatible_timeout_default(tmp_path) -> None:
    engine = _fresh_engine(tmp_path, "legacy-v6.db")
    _insert_investigation(engine, "inv-legacy-v6")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE runtime_runs DROP COLUMN timeout_seconds"
        )
        connection.exec_driver_sql(
            """
            INSERT INTO runtime_runs (
                id, investigation_id, run_kind, strategy, status, run_reason,
                created_at, lease_version, next_event_sequence
            ) VALUES (
                'run-legacy-v6', 'inv-legacy-v6', 'live', 'fixed', 'created',
                'initial', '2026-07-17T00:00:00+00:00', 0, 0
            )
            """
        )

    initialize_database(engine)

    with engine.connect() as connection:
        row = connection.execute(
            select(runtime_runs).where(runtime_runs.c.id == "run-legacy-v6")
        ).mappings().one()
    assert row["timeout_seconds"] == 60


def test_current_v6_runtime_upgrades_to_the_fresh_v7_manifest(tmp_path) -> None:
    engine = _fresh_engine(tmp_path, "current-v6.db")
    _insert_investigation(engine, "inv-current-v6")
    with engine.begin() as connection:
        connection.execute(
            runtime_runs.insert().values(
                **_run_values("run-current-v6", "inv-current-v6")
            )
        )
        connection.execute(schema_version.delete())
        connection.execute(schema_version.insert().values(version=6))
        for column in (
            "execution_contract",
            "authority_mode",
            "execution_contract_version",
        ):
            connection.exec_driver_sql(
                f"ALTER TABLE runtime_runs DROP COLUMN {column}"
            )

    initialize_database(engine)

    fresh_engine = _fresh_engine(tmp_path, "fresh-v7.db")
    fresh = {
        item["name"]
        for item in inspect(fresh_engine).get_columns("runtime_runs")
    }
    upgraded = {item["name"] for item in inspect(engine).get_columns("runtime_runs")}
    assert upgraded == fresh
    with engine.connect() as connection:
        row = connection.execute(
            select(runtime_runs).where(runtime_runs.c.id == "run-current-v6")
        ).mappings().one()
    assert row["execution_contract_version"] == "v10_legacy"
    assert row["authority_mode"] == "legacy_deterministic"
    assert row["execution_contract"]["run_kind"] == "live"


def test_runtime_event_and_attempt_sequences_are_unique(tmp_path) -> None:
    engine = _fresh_engine(tmp_path, "unique.db")
    _insert_investigation(engine, "inv-1")
    with engine.begin() as connection:
        connection.execute(insert(runtime_runs).values(**_run_values("run-1", "inv-1")))
        connection.execute(
            insert(runtime_attempts).values(**_attempt_values("attempt-1", "run-1", 1))
        )
        connection.execute(
            insert(runtime_events).values(**_event_values("event-1", "run-1", "attempt-1", 1))
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                insert(runtime_events).values(
                    **_event_values("event-2", "run-1", "attempt-1", 1)
                )
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                insert(runtime_attempts).values(
                    **_attempt_values("attempt-2", "run-1", 1)
                )
            )


def test_runtime_foreign_keys_cover_run_attempt_checkpoint_and_parent_links(
    tmp_path,
) -> None:
    engine = _fresh_engine(tmp_path, "foreign-keys.db")
    _insert_investigation(engine, "inv-1")
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                insert(runtime_runs).values(
                    **_run_values("run-invalid", "inv-1", parent_run_id="missing")
                )
            )
        connection.execute(
            insert(runtime_runs).values(
                **_run_values("run-parent", "inv-1", status="completed")
            )
        )
        connection.execute(
            insert(runtime_runs).values(
                **_run_values("run-child", "inv-1", parent_run_id="run-parent")
            )
        )
        connection.execute(
            insert(runtime_attempts).values(
                **_attempt_values("attempt-child", "run-child", 1)
            )
        )
        connection.execute(
            insert(runtime_checkpoints).values(
                id="checkpoint-1",
                run_id="run-child",
                attempt_id="attempt-child",
                completed_phase="intake",
                event_sequence=2,
                state_digest="a" * 64,
                resume_state={},
                created_at=_now(),
                schema_version=1,
            )
        )
        connection.execute(
            runtime_runs.update()
            .where(runtime_runs.c.id == "run-child")
            .values(latest_checkpoint_id="checkpoint-1")
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_only_one_active_live_run_per_investigation(tmp_path) -> None:
    engine = _fresh_engine(tmp_path, "active.db")
    _insert_investigation(engine, "inv-1")
    with engine.begin() as connection:
        connection.execute(insert(runtime_runs).values(**_run_values("run-1", "inv-1")))
        with pytest.raises(IntegrityError):
            connection.execute(
                insert(runtime_runs).values(**_run_values("run-2", "inv-1"))
            )
        connection.execute(
            runtime_runs.update()
            .where(runtime_runs.c.id == "run-1")
            .values(status="completed", completed_at=_now())
        )
        connection.execute(insert(runtime_runs).values(**_run_values("run-3", "inv-1")))


def test_runtime_indexes_include_history_lease_and_event_catchup(tmp_path) -> None:
    engine = _fresh_engine(tmp_path, "indexes.db")
    inspector = inspect(engine)

    run_indexes = {item["name"] for item in inspector.get_indexes("runtime_runs")}
    attempt_indexes = {
        item["name"] for item in inspector.get_indexes("runtime_attempts")
    }
    event_indexes = {item["name"] for item in inspector.get_indexes("runtime_events")}

    assert "ix_runtime_runs_investigation_created_at" in run_indexes
    assert "ix_runtime_runs_lease_expires_at" in run_indexes
    assert "uq_runtime_runs_one_active_live_per_investigation" in run_indexes
    assert "uq_runtime_attempts_one_active_per_run" in attempt_indexes
    assert "ix_runtime_events_run_sequence" in event_indexes


def _fresh_engine(tmp_path, name: str):
    engine = create_db_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return engine


def _create_v5_fixture(engine, *, investigation_id: str) -> None:
    with engine.begin() as connection:
        for table in metadata.tables.values():
            if table.name not in _RUNTIME_TABLE_NAMES:
                table.create(connection)
        connection.execute(insert(schema_version).values(version=5))
    _insert_investigation(engine, investigation_id)


def _insert_investigation(engine, investigation_id: str) -> None:
    event = IncidentEvent(
        source=IncidentSource.WEBHOOK,
        service="checkout-service",
        environment="prod",
        severity=Severity.WARNING,
        title="runtime migration",
        description="historical record",
        started_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    with engine.begin() as connection:
        connection.execute(
            insert(investigations).values(
                id=investigation_id,
                event=event.model_dump(mode="json"),
                status="completed",
                failure_reason=None,
                created_at=_now(),
                updated_at=_now(),
                completed_at=_now(),
            )
        )


def _run_values(
    run_id: str,
    investigation_id: str,
    *,
    status: str = "created",
    parent_run_id: str | None = None,
) -> dict:
    return {
        "id": run_id,
        "investigation_id": investigation_id,
        "run_kind": "live",
        "strategy": "fixed",
        "status": status,
        "run_reason": "initial",
        "parent_run_id": parent_run_id,
        "created_at": _now(),
        "lease_version": 0,
        "next_event_sequence": 0,
    }


def _attempt_values(attempt_id: str, run_id: str, attempt_number: int) -> dict:
    return {
        "id": attempt_id,
        "run_id": run_id,
        "attempt_number": attempt_number,
        "status": "running",
        "started_at": _now(),
    }


def _event_values(
    event_id: str, run_id: str, attempt_id: str, sequence: int
) -> dict:
    return {
        "id": event_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "sequence": sequence,
        "event_type": "run.started",
        "actor_type": "runtime",
        "evidence_ids": [],
        "safe_payload": {},
        "occurred_at": _now(),
        "schema_version": 1,
    }


def _now() -> str:
    return datetime(2026, 7, 17, tzinfo=UTC).isoformat()
