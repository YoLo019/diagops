from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError

from backend.db.schema import runtime_runs
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeLeaseLost,
    RuntimeStore,
    RuntimeTerminalCommit,
    RuntimeTerminalEvent,
)


def test_create_get_and_list_runs_newest_first(runtime_store) -> None:
    older = _run("run-old", created_at=datetime(2026, 7, 17, 1, tzinfo=UTC))
    newer = _run("run-new", created_at=datetime(2026, 7, 17, 2, tzinfo=UTC))
    runtime_store.create_run(older)
    runtime_store.transition_run(
        older.id,
        expected=RuntimeRunStatus.CREATED,
        target=RuntimeRunStatus.CANCELLED,
    )
    runtime_store.create_run(newer)

    assert runtime_store.get_run(newer.id).id == newer.id
    assert [item.id for item in runtime_store.list_runs("inv-1")] == [newer.id, older.id]


def test_only_one_active_live_run_is_allowed(runtime_store) -> None:
    runtime_store.create_run(_run("run-1"))

    with pytest.raises(RuntimeConflict):
        runtime_store.create_run(_run("run-2"))


def test_invalid_store_transition_uses_typed_conflict(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-1"))

    with pytest.raises(RuntimeConflict):
        runtime_store.transition_run(
            run.id,
            expected=RuntimeRunStatus.CREATED,
            target=RuntimeRunStatus.COMPLETED,
        )


def test_terminal_commit_rolls_back_status_attempt_and_events_on_mid_fault(
    runtime_store,
) -> None:
    run = runtime_store.create_run(_run("run-terminal-rollback"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )

    def fail(point: str) -> None:
        if point == "persistence_mid_transaction":
            raise RuntimeError("terminal mid transaction")

    runtime_store._failpoint = fail
    with pytest.raises(Exception, match="terminal"):
        runtime_store.commit_terminal(
            RuntimeTerminalCommit(
                run_id=run.id,
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
        )

    assert runtime_store.get_run(run.id).status == RuntimeRunStatus.RUNNING
    assert runtime_store.get_attempt(attempt.id).status == RuntimeAttemptStatus.RUNNING
    assert runtime_store.list_events(run.id) == []


def test_terminal_freeze_failure_is_atomic_for_memory_and_sqlite(
    runtime_store, monkeypatch
) -> None:
    import backend.runtime.diff as diff_module
    import backend.runtime.sqlite_store as sqlite_store_module

    run = runtime_store.create_run(_run("run-terminal-freeze-failure"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            id="attempt-terminal-freeze-failure",
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    before_run = runtime_store.get_run(run.id)
    before_attempt = runtime_store.get_attempt(attempt.id)
    before_record = runtime_store.investigation_repository.get("inv-1")
    before_events = runtime_store.list_events(run.id)
    before_checkpoints = runtime_store.list_checkpoints(run.id)
    before_frozen = runtime_store.get_frozen_business_projection(run.id)

    def fail_freeze(*_args, **_kwargs):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(diff_module, "freeze_business_projection", fail_freeze)
    monkeypatch.setattr(
        sqlite_store_module, "freeze_business_projection", fail_freeze
    )
    with pytest.raises(RuntimeError, match="freeze failed"):
        runtime_store.commit_terminal(
            RuntimeTerminalCommit(
                run_id=run.id,
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
        )

    assert runtime_store.get_run(run.id) == before_run
    assert runtime_store.get_attempt(attempt.id) == before_attempt
    assert runtime_store.investigation_repository.get("inv-1") == before_record
    assert runtime_store.list_events(run.id) == before_events
    assert runtime_store.list_checkpoints(run.id) == before_checkpoints
    assert runtime_store.get_frozen_business_projection(run.id) == before_frozen


def test_expired_lease_interrupts_nonterminal_tool_calls(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-tool-interrupted"))
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
    runtime_store.investigation_repository.save_tool_calls(
        "inv-1",
        [
            ToolCallRecord(
                id="tool-running",
                task_id="task-1",
                agent_name="LogAgent",
                tool_name="read_logs",
                status=ToolCallStatus.RUNNING,
                runtime_run_id=run.id,
            )
        ],
    )
    expired = datetime(2026, 7, 16, tzinfo=UTC)
    if hasattr(runtime_store, "_runs"):
        runtime_store._runs[run.id] = leased.model_copy(
            update={"lease_expires_at": expired}
        )
    else:
        with runtime_store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(lease_expires_at=expired.isoformat())
            )

    runtime_store.audit_expired_leases(datetime(2026, 7, 17, tzinfo=UTC))

    call = runtime_store.investigation_repository.list_tool_calls("inv-1")[0]
    assert call.status == ToolCallStatus.INTERRUPTED
    assert call.completed_at == datetime(2026, 7, 17, tzinfo=UTC)


def test_memory_runtime_store_shares_repository_transaction_lock(runtime_store) -> None:
    if not hasattr(runtime_store, "_lock"):
        return

    assert runtime_store._lock is runtime_store.investigation_repository.transaction_lock


def test_attempt_numbers_are_strictly_consecutive(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-1"))
    leased, first = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-1", run.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.transition_attempt(
        first.id,
        expected=RuntimeAttemptStatus.RUNNING,
        target=RuntimeAttemptStatus.COMPLETED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )

    with pytest.raises(RuntimeConflict):
        runtime_store.create_attempt(
            _attempt("attempt-3", run.id, 3),
            owner="worker-a",
            lease_version=leased.lease_version,
        )

    second = runtime_store.create_attempt(
        _attempt("attempt-2", run.id, 2),
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    assert second.attempt_number == 2


def test_lease_acquire_renew_and_stale_fencing(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-1"))
    leased, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-1", run.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    assert leased.status == RuntimeRunStatus.RUNNING
    assert leased.lease_version == 1
    assert attempt.status == RuntimeAttemptStatus.RUNNING

    renewed = runtime_store.renew_lease(
        run.id,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    assert renewed is not None
    assert renewed.lease_expires_at >= leased.lease_expires_at
    assert (
        runtime_store.renew_lease(
            run.id,
            owner="worker-b",
            lease_version=leased.lease_version,
        )
        is None
    )
    with pytest.raises(RuntimeLeaseLost):
        runtime_store.transition_run(
            run.id,
            expected=RuntimeRunStatus.RUNNING,
            target=RuntimeRunStatus.COMPLETED,
            owner="worker-b",
            lease_version=leased.lease_version,
        )


def test_events_are_contiguous_ordered_and_paginated(runtime_store) -> None:
    run, attempt = _leased_attempt(runtime_store)
    runtime_store._append_event_for_test(_event("event-1", run.id, attempt.id, 1))
    runtime_store._append_event_for_test(_event("event-2", run.id, attempt.id, 2))

    with pytest.raises(RuntimeConflict):
        runtime_store._append_event_for_test(_event("event-4", run.id, attempt.id, 4))

    assert [event.sequence for event in runtime_store.list_events(run.id)] == [1, 2]
    assert [
        event.sequence for event in runtime_store.list_events(run.id, after=1, limit=1)
    ] == [2]


def test_cancel_request_and_expired_lease_audit(runtime_store) -> None:
    created = runtime_store.create_run(_run("run-created"))
    cancelled = runtime_store.request_cancel(created.id)
    assert cancelled.status == RuntimeRunStatus.CANCELLED

    expiring = runtime_store.create_run(_run("run-expiring", investigation_id="inv-2"))
    leased, _attempt_record = runtime_store.acquire_lease_and_create_attempt(
        expiring.id,
        attempt=_attempt("attempt-expiring", expiring.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    interrupted = runtime_store.audit_expired_leases(
        leased.lease_expires_at + timedelta(seconds=1)
    )
    assert [item.id for item in interrupted] == [expiring.id]
    assert runtime_store.get_run(expiring.id).status == RuntimeRunStatus.INTERRUPTED
    assert (
        runtime_store.get_attempt(_attempt_record.id).status
        == RuntimeAttemptStatus.INTERRUPTED
    )
    assert [
        item.event_type for item in runtime_store.list_events(expiring.id)
    ] == [
        RuntimeEventType.RUN_INTERRUPTED,
        RuntimeEventType.ATTEMPT_INTERRUPTED,
    ]
    rejection = runtime_store.append_recovery_rejection(
        expiring.id,
        attempt_id=_attempt_record.id,
        checkpoint_id=None,
    )
    assert rejection.event_type == RuntimeEventType.RECOVERY_REJECTED
    assert rejection.sequence == 3


def test_cross_investigation_parent_run_is_rejected(runtime_store) -> None:
    parent = runtime_store.create_run(_run("run-parent", investigation_id="inv-1"))
    runtime_store.transition_run(
        parent.id,
        expected=RuntimeRunStatus.CREATED,
        target=RuntimeRunStatus.CANCELLED,
    )

    with pytest.raises(RuntimeConflict):
        runtime_store.create_run(
            _run("run-child", investigation_id="inv-2", parent_run_id=parent.id)
        )


def test_live_rerun_parent_must_be_a_terminal_live_run(runtime_store) -> None:
    source = runtime_store.create_run(
        _run("run-parent-source").model_copy(
            update={
                "status": RuntimeRunStatus.COMPLETED,
                "completed_at": datetime.now(UTC),
            }
        )
    )
    replay = runtime_store.create_run(
        RuntimeRun(
            id="run-parent-replay",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.REPLAY,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.REPLAY,
            source_run_id=source.id,
            completed_at=datetime.now(UTC),
        )
    )

    with pytest.raises(RuntimeConflict, match="parent"):
        runtime_store.create_run(
            RuntimeRun(
                id="run-child-of-replay",
                investigation_id="inv-1",
                run_kind=RuntimeRunKind.LIVE,
                strategy=InvestigationStrategy.FIXED,
                run_reason=RuntimeRunReason.MANUAL_RERUN,
                parent_run_id=replay.id,
            )
        )


def test_only_one_resume_acquires_interrupted_run(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-1"))
    leased, _first = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-1", run.id, 1),
        owner="initial-worker",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.audit_expired_leases(leased.lease_expires_at + timedelta(seconds=1))
    barrier = Barrier(2)

    def acquire(owner: str) -> bool:
        barrier.wait()
        try:
            runtime_store.acquire_lease_and_create_attempt(
                run.id,
                attempt=_attempt(f"attempt-{owner}", run.id, 2),
                owner=owner,
                expected_status=RuntimeRunStatus.INTERRUPTED,
            )
        except RuntimeConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ("worker-a", "worker-b")))
    assert sorted(results) == [False, True]
    attempts = runtime_store.list_attempts(run.id)
    assert len(attempts) == 2
    assert sum(item.status == RuntimeAttemptStatus.RUNNING for item in attempts) == 1


def test_interrupted_run_cannot_transition_without_conditional_lease_acquire(
    runtime_store,
) -> None:
    run = runtime_store.create_run(_run("run-1"))
    leased, _attempt_record = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-1", run.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    runtime_store.audit_expired_leases(leased.lease_expires_at + timedelta(seconds=1))

    with pytest.raises(RuntimeLeaseLost):
        runtime_store.transition_run(
            run.id,
            expected=RuntimeRunStatus.INTERRUPTED,
            target=RuntimeRunStatus.RUNNING,
        )


def test_store_revalidates_event_after_model_mutation(runtime_store) -> None:
    run, attempt = _leased_attempt(runtime_store)
    event = _event("event-1", run.id, attempt.id, 1)
    event.safe_payload["prompt"] = "secret"

    with pytest.raises(ValidationError):
        runtime_store._append_event_for_test(event)


def test_runtime_store_protocol_and_implementations_hide_bypass_methods(
    runtime_store,
) -> None:
    assert "append_event" not in RuntimeStore.__dict__
    assert "acquire_lease" not in RuntimeStore.__dict__
    assert not hasattr(runtime_store, "append_event")
    assert not hasattr(runtime_store, "acquire_lease")


def test_only_one_active_attempt_and_fenced_attempt_transition(runtime_store) -> None:
    run = runtime_store.create_run(_run("run-1"))
    leased, first = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-1", run.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    with pytest.raises(RuntimeConflict):
        runtime_store.create_attempt(
            _attempt("attempt-2", run.id, 2),
            owner="worker-a",
            lease_version=leased.lease_version,
        )

    completed = runtime_store.transition_attempt(
        first.id,
        expected=RuntimeAttemptStatus.RUNNING,
        target=RuntimeAttemptStatus.COMPLETED,
        owner="worker-a",
        lease_version=leased.lease_version,
    )
    assert completed.status == RuntimeAttemptStatus.COMPLETED
    assert runtime_store.get_attempt(first.id).completed_at is not None


@pytest.mark.parametrize(
    ("run_target", "attempt_target"),
    [
        (RuntimeRunStatus.COMPLETED, RuntimeAttemptStatus.COMPLETED),
        (RuntimeRunStatus.FAILED, RuntimeAttemptStatus.FAILED),
    ],
)
def test_terminal_run_ends_active_attempt_atomically(
    runtime_store, run_target, attempt_target
) -> None:
    run, attempt = _leased_attempt(runtime_store)
    runtime_store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=run_target,
        owner="worker-a",
        lease_version=run.lease_version,
    )

    assert runtime_store.get_attempt(attempt.id).status == attempt_target


def test_cancelled_run_ends_active_attempt(runtime_store) -> None:
    run, attempt = _leased_attempt(runtime_store)
    cancelling = runtime_store.request_cancel(run.id)
    runtime_store.transition_run(
        run.id,
        expected=RuntimeRunStatus.CANCELLING,
        target=RuntimeRunStatus.CANCELLED,
        owner="worker-a",
        lease_version=cancelling.lease_version,
    )

    assert runtime_store.get_attempt(attempt.id).status == RuntimeAttemptStatus.CANCELLED


def test_concurrent_resume_creates_exactly_one_new_attempt(runtime_store) -> None:
    run, _attempt_one = _leased_attempt(runtime_store)
    runtime_store.audit_expired_leases(run.lease_expires_at + timedelta(seconds=1))
    barrier = Barrier(2)

    def resume(number: int) -> tuple[int, bool]:
        barrier.wait()
        try:
            runtime_store.acquire_lease_and_create_attempt(
                run.id,
                attempt=_attempt(f"attempt-{number}", run.id, 2),
                owner=f"worker-{number}",
                expected_status=RuntimeRunStatus.INTERRUPTED,
            )
        except RuntimeConflict:
            return number, False
        return number, True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume, (2, 3)))

    assert sorted(success for _number, success in results) == [False, True]
    attempts = runtime_store.list_attempts(run.id)
    assert len(attempts) == 2
    assert sum(item.status == RuntimeAttemptStatus.RUNNING for item in attempts) == 1
    winner = next(number for number, success in results if success)
    loser = next(number for number, success in results if not success)
    assert runtime_store.get_run(run.id).lease_owner == f"worker-{winner}"
    assert f"attempt-{winner}" in {item.id for item in attempts}
    assert f"attempt-{loser}" not in {item.id for item in attempts}


def _leased_attempt(runtime_store):
    created = runtime_store.create_run(_run("run-1"))
    return runtime_store.acquire_lease_and_create_attempt(
        created.id,
        attempt=_attempt("attempt-1", created.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )


def _run(
    run_id: str,
    *,
    investigation_id: str = "inv-1",
    created_at: datetime | None = None,
    parent_run_id: str | None = None,
) -> RuntimeRun:
    return RuntimeRun(
        id=run_id,
        investigation_id=investigation_id,
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        status=RuntimeRunStatus.CREATED,
        run_reason=(
            RuntimeRunReason.MANUAL_RERUN
            if parent_run_id is not None
            else RuntimeRunReason.INITIAL
        ),
        parent_run_id=parent_run_id,
        created_at=created_at or datetime.now(UTC),
    )


def _attempt(attempt_id: str, run_id: str, number: int) -> RuntimeAttempt:
    return RuntimeAttempt(
        id=attempt_id,
        run_id=run_id,
        attempt_number=number,
        status=RuntimeAttemptStatus.RUNNING,
    )


def _event(event_id: str, run_id: str, attempt_id: str, sequence: int) -> RuntimeEvent:
    return RuntimeEvent(
        id=event_id,
        run_id=run_id,
        attempt_id=attempt_id,
        sequence=sequence,
        event_type=RuntimeEventType.RUN_STARTED,
        actor_type=RuntimeActorType.RUNTIME,
    )
