import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttemptStatus,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunStatus,
)
from backend.runtime.phases import (
    BusinessMutation,
    PhaseCommit,
    checkpoint_digest,
)
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeIntegrityError,
    RuntimeLeaseLost,
    RuntimePersistenceError,
)
from backend.runtime.writer import RuntimeEventCommand, RuntimeWriter
from tests.runtime.conftest import _investigation
from tests.runtime.test_memory_store import _attempt, _run


class Failpoint:
    def __init__(self) -> None:
        self.target: str | None = None

    def raise_at(self, target: str) -> None:
        self.target = target

    def __call__(self, point: str) -> None:
        if point == self.target:
            raise RuntimeError(f"injected failure: {point}")


def test_writer_restarts_after_compatibility_client_closes_event_loop() -> None:
    writer = RuntimeWriter(object())

    async def start_on_short_lived_loop() -> None:
        await writer.start()
        await asyncio.sleep(0)

    asyncio.run(start_on_short_lived_loop())

    async def restart_on_new_loop() -> None:
        await writer.start()
        await asyncio.sleep(0)
        assert writer._consumer is not None
        assert not writer._consumer.done()
        await writer.shutdown()

    asyncio.run(restart_on_new_loop())


def test_checkpoint_digest_is_stable_and_excludes_business_payload() -> None:
    state = RuntimeResumeState(
        completed_evidence_ids=["evidence-b", "evidence-a"],
        remaining_tool_budget=2,
        successful_tool_keys=["tool-b", "tool-a"],
    )

    first = checkpoint_digest(
        run_id="run-1",
        attempt_id="attempt-1",
        completed_phase=RuntimePhase.EVIDENCE_COLLECTION,
        resume_state=state,
    )
    second = checkpoint_digest(
        run_id="run-1",
        attempt_id="attempt-1",
        completed_phase=RuntimePhase.EVIDENCE_COLLECTION,
        resume_state=state.model_copy(
            update={
                "completed_evidence_ids": ["evidence-a", "evidence-b"],
                "successful_tool_keys": ["tool-a", "tool-b"],
            }
        ),
    )

    assert first == second
    assert len(first) == 64


def test_phase_commit_is_all_or_nothing(tmp_path) -> None:
    failpoint = Failpoint()
    store = _sqlite_store(tmp_path, failpoint=failpoint)
    run, attempt = _running_attempt(store)
    prior_events = store.list_events(run.id)
    updated = store.investigation_repository.get("inv-1").model_copy(deep=True)
    evidence = _evidence("evidence-1")
    updated.evidence.append(evidence)
    failpoint.raise_at("after_business_before_event")

    with pytest.raises(RuntimePersistenceError):
        store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=run.lease_version,
                phase=RuntimePhase.INTAKE,
                business_mutation=BusinessMutation(
                    investigation_id="inv-1", investigation=updated
                ),
                safe_payload={"evidence_count": 1},
                resume_state=RuntimeResumeState(
                    completed_evidence_ids=[evidence.id]
                ),
            )
        )

    assert store.investigation_repository.get("inv-1").evidence == []
    assert store.list_events(run.id) == prior_events
    assert store.list_checkpoints(run.id) == []


def test_attempt_start_rolls_back_lease_when_creation_crashes(tmp_path) -> None:
    failpoint = Failpoint()
    store = _sqlite_store(tmp_path, failpoint=failpoint)
    created = store.create_run(_run("run-1"))
    failpoint.raise_at("after_lease_before_attempt")

    with pytest.raises(RuntimePersistenceError):
        store.acquire_lease_and_create_attempt(
            created.id,
            attempt=_attempt("attempt-1", created.id, 1),
            owner="worker-a",
            expected_status=RuntimeRunStatus.CREATED,
        )

    restored = store.get_run(created.id)
    assert restored.status == RuntimeRunStatus.CREATED
    assert restored.lease_owner is None
    assert store.list_attempts(created.id) == []


def test_successful_phase_commit_persists_business_events_and_checkpoint(
    runtime_store,
) -> None:
    run, attempt = _running_attempt(runtime_store)
    checkpoint = runtime_store.commit_phase(
        PhaseCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="worker-a",
            lease_version=run.lease_version,
            phase=RuntimePhase.INTAKE,
            business_mutation=BusinessMutation(investigation_id="inv-1"),
            safe_payload={"evidence_count": 0},
            resume_state=RuntimeResumeState(),
        )
    )

    assert checkpoint.event_sequence == 2
    assert [event.sequence for event in runtime_store.list_events(run.id)] == [1, 2]
    assert [event.event_type.value for event in runtime_store.list_events(run.id)] == [
        "phase.completed",
        "checkpoint.created",
    ]
    persisted = runtime_store.get_run(run.id)
    assert persisted.current_phase == RuntimePhase.INTAKE
    assert persisted.latest_checkpoint_id == checkpoint.id


def test_phase_commit_rejects_stale_lease(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    runtime_store.audit_expired_leases(run.lease_expires_at + timedelta(seconds=1))
    replacement, _replacement_attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=_attempt("attempt-2", run.id, 2),
        owner="worker-b",
        expected_status=RuntimeRunStatus.INTERRUPTED,
    )

    with pytest.raises(RuntimeLeaseLost):
        runtime_store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=run.lease_version,
                phase=RuntimePhase.INTAKE,
                business_mutation=BusinessMutation(investigation_id="inv-1"),
                safe_payload={},
                resume_state=RuntimeResumeState(),
            )
        )


def test_runtime_writer_serializes_concurrent_phase_results(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)

    async def scenario() -> None:
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        first = _phase_commit(run, attempt, RuntimePhase.INTAKE)
        second = _phase_commit(
            run,
            attempt,
            RuntimePhase.EVIDENCE_COLLECTION,
            expected_checkpoint_id=first.checkpoint_id,
            expected_previous_phase=RuntimePhase.INTAKE,
        )
        commits = [first, second]
        checkpoints = await asyncio.gather(*(writer.submit(item) for item in commits))
        await writer.shutdown()
        assert [item.event_sequence for item in checkpoints] == [2, 4]
        with pytest.raises(RuntimePersistenceError):
            await writer.submit(commits[0])

    asyncio.run(scenario())
    assert [event.sequence for event in runtime_store.list_events(run.id)] == [1, 2, 3, 4]


def test_runtime_writer_does_not_block_heartbeat_during_phase_commit(runtime_store):
    run, attempt = _running_attempt(runtime_store)
    started = threading.Event()
    released = threading.Event()
    release_at: list[float] = []
    original_commit_phase = runtime_store.commit_phase

    def blocked_commit(commit):
        started.set()
        assert released.wait(2)
        return original_commit_phase(commit)

    runtime_store.commit_phase = blocked_commit

    async def scenario() -> None:
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        tick_at: list[float] = []
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, lambda: tick_at.append(time.monotonic()))

        def release_commit() -> None:
            assert started.wait(1)
            time.sleep(0.2)
            release_at.append(time.monotonic())
            released.set()

        releaser = threading.Thread(target=release_commit)
        releaser.start()
        try:
            await writer.submit(_phase_commit(run, attempt, RuntimePhase.INTAKE))
        finally:
            await writer.shutdown()
            releaser.join()

        assert tick_at
        assert release_at
        assert tick_at[0] < release_at[0]

    asyncio.run(scenario())


def test_writer_allocates_contiguous_sequences_for_concurrent_producers(
    runtime_store,
) -> None:
    run, attempt = _running_attempt(runtime_store)

    async def scenario() -> None:
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        commands = [
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-a",
                lease_version=run.lease_version,
                event_type=event_type,
                actor_type=actor_type,
                actor_name=f"producer-{index}",
                safe_payload={"status": "completed"},
            )
            for index, (event_type, actor_type) in enumerate(
                (
                    (RuntimeEventType.AGENT_COMPLETED, RuntimeActorType.AGENT),
                    (RuntimeEventType.TOOL_COMPLETED, RuntimeActorType.TOOL),
                    (RuntimeEventType.MODEL_COMPLETED, RuntimeActorType.MODEL),
                )
                * 10
            )
        ]
        events = await asyncio.gather(*(writer.submit_event(item) for item in commands))
        await writer.shutdown()
        assert sorted(event.sequence for event in events) == list(range(1, 31))

    asyncio.run(scenario())
    assert [event.sequence for event in runtime_store.list_events(run.id)] == list(
        range(1, 31)
    )


def test_writer_event_rejects_lost_lease_terminal_run_and_stale_attempt(
    runtime_store,
) -> None:
    run, attempt = _running_attempt(runtime_store)
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.AGENT_STARTED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={},
    )

    async def submit_once(item):
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        try:
            return await writer.submit_event(item)
        finally:
            await writer.shutdown()

    stale = replace(command, lease_version=999)
    with pytest.raises(RuntimeLeaseLost):
        asyncio.run(submit_once(stale))

    wrong_owner = replace(command, lease_owner="worker-b")
    with pytest.raises(RuntimeLeaseLost):
        asyncio.run(submit_once(wrong_owner))

    runtime_store.transition_attempt(
        attempt.id,
        expected=RuntimeAttemptStatus.RUNNING,
        target=RuntimeAttemptStatus.COMPLETED,
        owner="worker-a",
        lease_version=run.lease_version,
    )
    with pytest.raises(RuntimeIntegrityError):
        asyncio.run(submit_once(command))

    runtime_store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.COMPLETED,
        owner="worker-a",
        lease_version=run.lease_version,
    )
    with pytest.raises(RuntimeConflict):
        asyncio.run(submit_once(command))


@pytest.mark.parametrize(
    "unsafe_payload",
    [
        {"tool_name": "read_logs", "normalized_inputs": {"query": "raw log query"}},
        {"tool_name": "read_logs", "metadata": {"data": {"body": "raw log body"}}},
        {"tool_name": "read_logs", "metadata": {"provider_response": "raw response"}},
        {"tool_name": "read_logs", "metadata": {"details": {"payload": {"body": "raw"}}}},
        {
            "tool_name": "read_logs",
            "normalized_inputs": {
                "reason": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            },
        },
        {
            "tool_name": "read_logs",
            "metadata": {"authorization": "Bearer persistence-secret"},
        },
        {
            "tool_name": "read_logs",
            "metadata": {"endpoint": "https://user:secret@example.test/logs"},
        },
    ],
)
def test_writer_refuses_unsafe_payload_before_persistence(
    runtime_store, unsafe_payload
) -> None:
    run, attempt = _running_attempt(runtime_store)
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.TOOL_COMPLETED,
        actor_type=RuntimeActorType.TOOL,
        safe_payload=unsafe_payload,
    )

    async def submit() -> None:
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        try:
            await writer.submit_event(command)
        finally:
            await writer.shutdown()

    with pytest.raises(ValidationError):
        asyncio.run(submit())
    assert runtime_store.list_events(run.id) == []


def test_writer_persists_allowlisted_tool_payload(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    safe_payload = {
        "tool_name": "read_logs",
        "normalized_inputs": {
            "start_time": "2026-07-17T00:00:00Z",
            "end_time": "2026-07-17T00:05:00Z",
            "limit": 20,
            "keywords": ["timeout"],
            "levels": ["ERROR"],
            "instance": "checkout-01",
        },
        "metadata": {"result_count": 1, "provider_status": "success"},
    }
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.TOOL_COMPLETED,
        actor_type=RuntimeActorType.TOOL,
        safe_payload=safe_payload,
    )

    async def submit():
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        try:
            return await writer.submit_event(command)
        finally:
            await writer.shutdown()

    persisted = asyncio.run(submit())
    assert persisted.safe_payload == safe_payload
    assert runtime_store.list_events(run.id)[0].safe_payload == safe_payload


@pytest.mark.parametrize(
    "unsafe_payload",
    [
        {"metadata": {"authorization": "Bearer future-secret"}},
        {"metadata": {"label": "x" * 300}},
        {"metadata": {"ratio": float("inf")}},
    ],
)
def test_future_event_opaque_metadata_keeps_common_safety_bounds(
    unsafe_payload,
) -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-future",
            attempt_id="attempt-future",
            sequence=1,
            event_type="future.event",
            actor_type="future.actor",
            schema_version=2,
            safe_payload=unsafe_payload,
        )


def test_writer_redacts_bare_provider_credential_before_readback(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    credential = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.RUN_FAILED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"message": f"provider failed {credential}"},
    )

    async def submit():
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        try:
            return await writer.submit_event(command)
        finally:
            await writer.shutdown()

    persisted = asyncio.run(submit())
    readback = runtime_store.list_events(run.id)[0]
    assert credential not in persisted.safe_payload["message"]
    assert credential not in readback.safe_payload["message"]
    assert "[REDACTED]" in readback.safe_payload["message"]


def test_writer_rejects_event_after_lease_expiry_audit(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    runtime_store.audit_expired_leases(run.lease_expires_at + timedelta(seconds=1))
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.AGENT_STARTED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={},
    )

    async def submit() -> None:
        writer = RuntimeWriter(runtime_store)
        await writer.start()
        try:
            await writer.submit_event(command)
        finally:
            await writer.shutdown()

    with pytest.raises(RuntimeConflict):
        asyncio.run(submit())
    assert [
        event.event_type for event in runtime_store.list_events(run.id)
    ] == [
        RuntimeEventType.RUN_INTERRUPTED,
        RuntimeEventType.ATTEMPT_INTERRUPTED,
    ]


def test_phase_commit_rejects_duplicate_and_out_of_order_without_new_events(
    runtime_store,
) -> None:
    run, attempt = _running_attempt(runtime_store)
    first = _phase_commit(run, attempt, RuntimePhase.INTAKE)
    checkpoint = runtime_store.commit_phase(first)
    event_count = len(runtime_store.list_events(run.id))

    assert runtime_store.commit_phase(first).id == checkpoint.id
    assert len(runtime_store.list_events(run.id)) == event_count
    with pytest.raises(RuntimeConflict):
        runtime_store.commit_phase(
            _phase_commit(
                run,
                attempt,
                RuntimePhase.DETERMINISTIC_RCA,
                expected_checkpoint_id=checkpoint.id,
                expected_previous_phase=RuntimePhase.INTAKE,
            )
        )
    assert len(runtime_store.list_events(run.id)) == event_count


def test_concurrent_identical_phase_commit_is_idempotent(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    commit = _phase_commit(run, attempt, RuntimePhase.INTAKE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoints = list(pool.map(lambda _index: runtime_store.commit_phase(commit), range(2)))

    assert {item.id for item in checkpoints} == {commit.checkpoint_id}
    assert [event.sequence for event in runtime_store.list_events(run.id)] == [1, 2]


def test_event_is_published_only_after_durable_commit(tmp_path) -> None:
    published = []
    store = _sqlite_store(tmp_path, event_publisher=lambda events: published.extend(events))
    run, attempt = _running_attempt(store)
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.AGENT_COMPLETED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={"status": "completed"},
    )

    event = store.append_event_command(command)

    assert published == [event]
    assert store.list_events(run.id) == [event]


def test_writer_submit_shutdown_race_never_leaves_waiter_pending(runtime_store) -> None:
    run, attempt = _running_attempt(runtime_store)
    command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        event_type=RuntimeEventType.AGENT_STARTED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={},
    )

    async def scenario() -> None:
        writer = RuntimeWriter(runtime_store, max_queue_size=1)
        await writer.start()
        submissions = [asyncio.create_task(writer.submit_event(command)) for _ in range(20)]
        await asyncio.sleep(0)
        await writer.shutdown()
        results = await asyncio.wait_for(
            asyncio.gather(*submissions, return_exceptions=True), timeout=2
        )
        assert len(results) == 20

    asyncio.run(scenario())


def _sqlite_store(tmp_path, *, failpoint=None, event_publisher=None) -> SQLiteRuntimeStore:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'writer.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(_investigation("inv-1"))
    return SQLiteRuntimeStore(
        engine,
        repository,
        failpoint=failpoint,
        event_publisher=event_publisher,
    )


def _running_attempt(store):
    created = store.create_run(_run("run-1"))
    return store.acquire_lease_and_create_attempt(
        created.id,
        attempt=_attempt("attempt-1", created.id, 1),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )


def _phase_commit(
    run,
    attempt,
    phase,
    *,
    expected_checkpoint_id=None,
    expected_previous_phase=None,
):
    return PhaseCommit(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="worker-a",
        lease_version=run.lease_version,
        phase=phase,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_previous_phase=expected_previous_phase,
        business_mutation=BusinessMutation(investigation_id="inv-1"),
        safe_payload={},
        resume_state=RuntimeResumeState(),
    )


def _evidence(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 17, tzinfo=UTC),
        summary="bounded runtime evidence",
        payload={"count": 1},
        confidence=0.8,
    )
