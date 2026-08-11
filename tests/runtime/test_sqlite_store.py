import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from backend.db.schema import runtime_runs
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunStatus,
)
from backend.runtime.phases import BusinessMutation, PhaseCommit
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeLeaseLost,
    RuntimeTerminalCommit,
    RuntimeTerminalEvent,
)


def test_sqlite_v11_model_turn_budget_survives_store_reload(runtime_store) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return

    from tests.runtime.test_v11_isolation_red import _v11_run

    run = runtime_store.create_run(_v11_run("run-sqlite-model-turn-budget"))
    leased, _attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )

    assert runtime_store.reserve_model_turn(
        run.id, owner="worker-a", lease_version=leased.lease_version
    ) == 7
    assert runtime_store.get_run(run.id).remaining_model_turns == 7
    for _ in range(6):
        runtime_store.reserve_model_turn(
            run.id, owner="worker-a", lease_version=leased.lease_version
        )
    assert runtime_store.reserve_model_turn(
        run.id, owner="worker-a", lease_version=leased.lease_version
    ) == 0
    with pytest.raises(RuntimeConflict, match="exhausted"):
        runtime_store.reserve_model_turn(
            run.id, owner="worker-a", lease_version=leased.lease_version
        )


def test_sqlite_persisted_v11_missing_model_turns_fails_closed(runtime_store) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return

    from tests.runtime.test_v11_isolation_red import _v11_run

    run = runtime_store.create_run(_v11_run("run-sqlite-missing-model-turns"))
    with runtime_store.engine.begin() as connection:
        connection.execute(
            runtime_runs.update()
            .where(runtime_runs.c.id == run.id)
            .values(remaining_model_turns=None)
        )
    with pytest.raises(ValueError, match="persisted remaining model turns"):
        runtime_store.get_run(run.id)


def test_sqlite_store_persists_run_in_typed_columns(runtime_store) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return

    from tests.runtime.test_memory_store import _run

    created = runtime_store.create_run(_run("run-sqlite"))
    with runtime_store.engine.connect() as connection:
        row = connection.execute(
            select(runtime_runs).where(runtime_runs.c.id == created.id)
        ).mappings().one()

    assert row["investigation_id"] == "inv-1"
    assert row["status"] == "created"
    assert row["timeout_seconds"] == created.timeout_seconds
    assert row["next_event_sequence"] == 0


def test_expired_lease_audit_loses_status_interleave_via_cas(runtime_store) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return

    from tests.runtime.test_memory_store import _leased_attempt

    leased, _attempt = _leased_attempt(runtime_store)
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with runtime_store.engine.begin() as connection:
        connection.execute(
            runtime_runs.update()
            .where(runtime_runs.c.id == leased.id)
            .values(lease_expires_at=expired_at.isoformat())
        )
    interleaved = False

    def complete_before_audit_update(
        _connection,
        cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal interleaved
        if interleaved or not statement.lstrip().startswith("UPDATE runtime_runs SET"):
            return
        if "interrupted" not in repr(parameters):
            return
        interleaved = True
        cursor.execute(
            "UPDATE runtime_runs SET status = 'completed' WHERE id = ?",
            (leased.id,),
        )

    event.listen(
        runtime_store.engine,
        "before_cursor_execute",
        complete_before_audit_update,
    )
    try:
        interrupted = runtime_store.audit_expired_leases(datetime.now(UTC))
    finally:
        event.remove(
            runtime_store.engine,
            "before_cursor_execute",
            complete_before_audit_update,
        )

    assert interleaved is True
    assert interrupted == []
    assert runtime_store.get_run(leased.id).status == RuntimeRunStatus.COMPLETED


def test_phase_commit_rechecks_lease_after_expensive_projection(
    runtime_store, monkeypatch
) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return
    from backend.runtime import phases
    from tests.runtime.test_memory_store import _leased_attempt

    runtime_store.lease_seconds = 1
    leased, attempt = _leased_attempt(runtime_store)
    before = runtime_store.investigation_repository.get("inv-1")
    original_digest = phases.durable_projection_digest

    def delayed_digest(*args, **kwargs):
        time.sleep(1.2)
        return original_digest(*args, **kwargs)

    monkeypatch.setattr(phases, "durable_projection_digest", delayed_digest)
    commit = PhaseCommit(
        run_id=leased.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=leased.lease_version,
        phase=RuntimePhase.INTAKE,
        business_mutation=BusinessMutation(
            investigation_id="inv-1",
            investigation=before.model_copy(update={"failure_reason": "must roll back"}),
        ),
        safe_payload={"status": "completed"},
        resume_state=RuntimeResumeState(),
    )

    with pytest.raises(RuntimeLeaseLost):
        runtime_store.commit_phase(commit)

    assert runtime_store.investigation_repository.get("inv-1") == before
    assert runtime_store.list_events(leased.id) == []
    assert runtime_store.list_checkpoints(leased.id) == []
    assert runtime_store.get_run(leased.id).current_phase is None
    assert runtime_store.get_attempt(attempt.id).status == RuntimeAttemptStatus.RUNNING


def test_terminal_commit_rechecks_lease_after_expensive_projection(
    runtime_store, monkeypatch
) -> None:
    if not isinstance(runtime_store, SQLiteRuntimeStore):
        return
    import backend.runtime.sqlite_store as sqlite_store_module
    from tests.runtime.test_memory_store import _leased_attempt

    runtime_store.lease_seconds = 1
    leased, attempt = _leased_attempt(runtime_store)
    before = runtime_store.investigation_repository.get("inv-1")
    original_freeze = sqlite_store_module.freeze_business_projection

    def delayed_freeze(*args, **kwargs):
        time.sleep(1.2)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        sqlite_store_module, "freeze_business_projection", delayed_freeze
    )
    commit = RuntimeTerminalCommit(
        run_id=leased.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=leased.lease_version,
        expected_run_status=RuntimeRunStatus.RUNNING,
        target_run_status=RuntimeRunStatus.COMPLETED,
        expected_attempt_status=RuntimeAttemptStatus.RUNNING,
        target_attempt_status=RuntimeAttemptStatus.COMPLETED,
        events=(
            RuntimeTerminalEvent(
                event_type=RuntimeEventType.RUN_COMPLETED,
                actor_type=RuntimeActorType.RUNTIME,
                safe_payload={"status": "completed"},
            ),
        ),
    )

    with pytest.raises(RuntimeLeaseLost):
        runtime_store.commit_terminal(commit)

    assert runtime_store.investigation_repository.get("inv-1") == before
    assert runtime_store.list_events(leased.id) == []
    assert runtime_store.get_run(leased.id).status == RuntimeRunStatus.RUNNING
    assert runtime_store.get_attempt(attempt.id).status == RuntimeAttemptStatus.RUNNING
