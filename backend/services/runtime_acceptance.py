from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from threading import Event as ThreadEvent
from threading import Lock as ThreadLock
from urllib.parse import urlsplit
from uuid import uuid4

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from backend.config.settings import OpenTelemetrySettings
from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import runtime_checkpoints, runtime_events
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.providers.results import ProviderResult
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.diff import RuntimeDiffService, canonical_diff_json
from backend.runtime.event_hub import EventHub
from backend.runtime.faults import (
    APPROVED_FAULT_POINTS,
    DeterministicFaultInjector,
    RuntimeInjectedFault,
)
from backend.runtime.manager import RuntimeManager
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import (
    V10_PHASE_ORDER,
    BusinessMutation,
    PhaseCommit,
    PhaseOutput,
    checkpoint_digest,
)
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import (
    InMemoryRuntimeStore,
    RuntimeConflict,
    RuntimeNotFound,
    RuntimePersistenceError,
)
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeEventCommand, RuntimeWriter
from backend.services.incident_cases import load_incident_case
from backend.tools.registry import ToolInvocationResult, ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SCENARIOS = APPROVED_FAULT_POINTS
SEED = 42
_CASES = (
    "deployment_regression",
    "traffic_spike",
    "dependency_timeout",
    "database_slowdown",
    "single_bad_instance",
)
_PROHIBITED_FIELD = re.compile(
    r'"(?:api_key|authorization|chain_of_thought|credential|credentials|'
    r'evidence_body|model_output|password|prompt|provider_payload|raw_output|'
    r'raw_payload|reasoning|request_payload|response_payload|secret)"\s*:',
    re.IGNORECASE,
)
_PROHIBITED_VALUE = re.compile(
    r"(?:authorization\s*:|bearer\s+[a-z0-9._-]+|(?<![a-z0-9])sk-[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|password|secret)\s*[=:]\s*\S+|"
    r"raw[ _-]+provider[ _-]+payload(?:[ _-]+marker)?)",
    re.IGNORECASE,
)
_URL_VALUE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)


def _event(investigation_id: str) -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.WARNING,
        title=f"runtime acceptance {investigation_id}",
        description="deterministic key-free runtime acceptance",
        started_at=datetime(2026, 7, 19, tzinfo=UTC),
        time_window_minutes=30,
    )


def _services(*investigation_ids: str):
    repository = InMemoryInvestigationRepository()
    for investigation_id in investigation_ids:
        repository.save(
            InvestigationRecord(
                id=investigation_id,
                event=_event(investigation_id),
                runtime_available=True,
            )
        )
    return repository, InMemoryRuntimeStore(repository, lease_seconds=5)


@contextmanager
def _temporary_sqlite_engine(prefix: str):
    with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
        engine = create_db_engine(f"sqlite:///{Path(temp_dir) / 'runtime.db'}")
        try:
            initialize_database(engine)
            yield engine
        finally:
            engine.dispose()


def _run(store, investigation_id: str, run_id: str, *, adaptive: bool = False):
    return store.create_run(
        RuntimeRun(
            id=run_id,
            investigation_id=investigation_id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=(
                InvestigationStrategy.ADAPTIVE
                if adaptive
                else InvestigationStrategy.FIXED
            ),
            run_reason=RuntimeRunReason.INITIAL,
        )
    )


def _assert_sequence(store, run_id: str) -> list[RuntimeEvent]:
    events = store.list_events(run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    return events


def _interrupt(store, coordinator: RuntimeCoordinator, run_id: str) -> None:
    store._runs[run_id] = store._runs[run_id].model_copy(
        update={"lease_expires_at": datetime(2026, 7, 18, tzinfo=UTC)}
    )
    interrupted = coordinator.audit_expired_leases(
        datetime(2026, 7, 19, tzinfo=UTC)
    )
    assert [run.id for run in interrupted] == [run_id]
    assert store.get_run(run_id).status == RuntimeRunStatus.INTERRUPTED
    assert store.get_run(run_id).lease_owner is None
    assert store.list_attempts(run_id)[-1].status == RuntimeAttemptStatus.INTERRUPTED


class _DeterministicProviderAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self) -> dict[str, object]:
        self.calls += 1
        return {"status": "success", "evidence_count": 1}


class _BoundaryExecutor:
    def __init__(self, point: str, repository) -> None:
        self.point = point
        self.repository = repository
        self.provider = _DeterministicProviderAdapter()
        self.model_calls = 0

    async def execute_phase(self, phase_input) -> PhaseOutput:
        if phase_input.phase == RuntimePhase.INTAKE:
            if self.point == "provider_before_commit":
                assert self.provider.collect()["status"] == "success"
                phase_input.hit_fault("provider_before_commit")
            elif self.point == "model_after_send":

                async def turn(**_kwargs):
                    self.model_calls += 1
                    return object()

                runtime = AgentsRcaRuntime(
                    model="deterministic-fake-model",
                    turn=turn,
                    check_execution=phase_input.check_execution,
                    persist_model_event=phase_input.persist_model_event,
                )
                runtime._hit_fault = phase_input.hit_fault
                await runtime._invoke_turn(None, marker="acceptance")
        return PhaseOutput(
            business_mutation=BusinessMutation(
                investigation_id=phase_input.investigation_id
            ),
            safe_payload={"status": "completed"},
            resume_state=phase_input.resume_state,
        )


async def _boundary_scenario(point: str) -> dict[str, object]:
    repository, store = _services(f"inv-{point}")
    run = _run(store, f"inv-{point}", f"run-{point}")
    original = repository.get(run.investigation_id)
    executor = _BoundaryExecutor(point, repository)
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
        fault_injector=DeterministicFaultInjector({point: 1}),
    )
    crashed = await coordinator.execute(run.id, owner="acceptance-a")
    assert crashed.status == RuntimeRunStatus.RUNNING
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.RUNNING
    if point == "provider_before_commit":
        assert executor.provider.calls == 1
        assert repository.get(run.investigation_id).evidence == original.evidence
    if point == "model_after_send":
        assert executor.model_calls == 1
        event_types = [event.event_type for event in store.list_events(run.id)]
        assert RuntimeEventType.MODEL_STARTED in event_types
        assert RuntimeEventType.MODEL_COMPLETED not in event_types
    _interrupt(store, coordinator, run.id)
    events = _assert_sequence(store, run.id)
    await coordinator.shutdown()
    return {
        "run_status": "interrupted",
        "attempt_status": "interrupted",
        "event_count": len(events),
        "checkpoint_count": len(store.list_checkpoints(run.id)),
        "external_call_count": executor.provider.calls + executor.model_calls,
        "lease_owner": None,
        "_privacy_surfaces": [
            json.dumps(
                [event.model_dump(mode="json") for event in events],
                ensure_ascii=False,
            )
        ],
    }


async def _persistence_mid_transaction() -> dict[str, object]:
    with _temporary_sqlite_engine("diagops-acceptance-transaction-") as engine:
        repository = SQLiteInvestigationRepository(engine)
        repository.save(
            InvestigationRecord(
                id="inv-transaction",
                event=_event("inv-transaction"),
                runtime_available=True,
            )
        )
        original = repository.get("inv-transaction")
        store = SQLiteRuntimeStore(engine, repository)
        run = _run(store, original.id, "run-transaction")
        leased, attempt = store.acquire_lease_and_create_attempt(
            run.id,
            attempt=RuntimeAttempt(
                run_id=run.id,
                attempt_number=1,
                status=RuntimeAttemptStatus.RUNNING,
            ),
            owner="acceptance-a",
            expected_status=RuntimeRunStatus.CREATED,
        )
        store.fault_injector = DeterministicFaultInjector(
            {"persistence_mid_transaction": 1}
        )
        writer = RuntimeWriter(store)
        await writer.start()
        before_event_count = len(store.list_events(run.id))
        try:
            await writer.submit(
                PhaseCommit(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner="acceptance-a",
                    lease_version=leased.lease_version,
                    phase=RuntimePhase.INTAKE,
                    business_mutation=BusinessMutation(
                        investigation_id=original.id,
                        investigation=original.model_copy(
                            update={"failure_reason": "must roll back"}
                        ),
                    ),
                    safe_payload={"status": "completed"},
                    resume_state=RuntimeResumeState(),
                )
            )
        except RuntimePersistenceError:
            pass
        else:
            raise AssertionError("SQLite phase transaction fault did not fire")
        await writer.shutdown()
        assert repository.get(original.id) == original
        assert len(store.list_events(run.id)) == before_event_count
        assert store.list_checkpoints(run.id) == []
        persisted = store.get_run(run.id)
        assert persisted.status == RuntimeRunStatus.RUNNING
        assert persisted.current_phase is None
        assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.RUNNING
        return {
            "run_status": "running",
            "attempt_status": "running",
            "business_rollback": True,
            "event_rollback_count": 0,
            "checkpoint_count": 0,
            "lease_owned": True,
            "external_call_count": 0,
        }


async def _tool_after_commit_before_checkpoint() -> dict[str, object]:
    repository, store = _services("inv-tool")
    run = _run(store, "inv-tool", "run-tool", adaptive=True)
    invocations = 0
    registry = ToolRegistry()

    def invoke(**kwargs) -> ToolInvocationResult:
        nonlocal invocations
        invocations += 1
        evidence = EvidenceItem(
            id="acceptance-tool-evidence",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 7, 19, tzinfo=UTC),
            summary="bounded deterministic evidence",
        )
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
                output_evidence_ids=[evidence.id],
            ),
            evidence=[evidence],
            provider_results=[],
        )

    registry.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True), invoke
    )

    class Executor:
        async def execute_phase(self, phase_input) -> PhaseOutput:
            state = phase_input.resume_state
            if phase_input.phase == RuntimePhase.INTAKE:
                session = AdaptiveToolSession(
                    event=repository.get("inv-tool").event,
                    seed_evidence=[],
                    registry=registry,
                    task_ids={AgentName.LOG: f"task-{phase_input.attempt_id}"},
                    runtime_run_id=phase_input.run_id,
                    resolve_tool_result=phase_input.resolve_tool_result,
                    persist_tool_start=phase_input.persist_tool_start,
                    persist_tool_result=phase_input.persist_tool_result,
                    check_execution=phase_input.check_execution,
                )
                await session.invoke(
                    AgentName.LOG,
                    "read_logs",
                    json.dumps(
                        {
                            "start_time": "2026-07-19T00:00:00Z",
                            "end_time": "2026-07-19T00:10:00Z",
                            "reason": "acceptance",
                        }
                    ),
                    1,
                )
                calls = [
                    call
                    for call in repository.list_tool_calls("inv-tool")
                    if call.status == ToolCallStatus.SUCCESS
                ]
                state = RuntimeResumeState(
                    completed_evidence_ids=[
                        item.id for item in repository.get("inv-tool").evidence
                    ],
                    successful_tool_keys=[
                        call.idempotency_key
                        for call in calls
                        if call.idempotency_key is not None
                    ],
                )
            return PhaseOutput(
                business_mutation=BusinessMutation(investigation_id="inv-tool"),
                safe_payload={"status": "completed"},
                resume_state=state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=Executor(),
        heartbeat_seconds=1,
        fault_injector=DeterministicFaultInjector(
            {"tool_after_commit_before_checkpoint": 1}
        ),
    )
    crashed = await coordinator.execute(run.id, owner="acceptance-a")
    assert crashed.status == RuntimeRunStatus.RUNNING
    assert invocations == 1
    assert len(repository.list_tool_calls("inv-tool")) == 1
    assert store.list_checkpoints(run.id) == []
    _interrupt(store, coordinator, run.id)
    started = time.perf_counter()
    completed = await coordinator.resume(run.id, owner="acceptance-b")
    recovery_ms = (time.perf_counter() - started) * 1000
    assert completed.status == RuntimeRunStatus.COMPLETED
    assert invocations == 1
    assert len(
        [
            call
            for call in repository.list_tool_calls("inv-tool")
            if call.status == ToolCallStatus.SUCCESS
        ]
    ) == 1
    assert [attempt.status for attempt in store.list_attempts(run.id)] == [
        RuntimeAttemptStatus.INTERRUPTED,
        RuntimeAttemptStatus.COMPLETED,
    ]
    events = _assert_sequence(store, run.id)
    await coordinator.shutdown()
    return {
        "run_status": "completed",
        "attempt_status": "completed",
        "successful_tool_reused": True,
        "external_call_count": invocations,
        "event_count": len(events),
        "checkpoint_count": len(store.list_checkpoints(run.id)),
        "recovery_wall_ms": round(recovery_ms, 3),
        "_privacy_surfaces": [
            json.dumps(
                [event.model_dump(mode="json") for event in events],
                ensure_ascii=False,
            )
        ],
    }


def _interrupted_run(store, investigation_id: str, run_id: str):
    run = _run(store, investigation_id, run_id)
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="acceptance-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store.transition_run(
        run.id,
        expected=RuntimeRunStatus.RUNNING,
        target=RuntimeRunStatus.INTERRUPTED,
        owner="acceptance-a",
        lease_version=leased.lease_version,
    )
    return run, attempt


class _CompleteExecutor:
    async def execute_phase(self, phase_input) -> PhaseOutput:
        await asyncio.sleep(0)
        return PhaseOutput(
            business_mutation=BusinessMutation(
                investigation_id=phase_input.investigation_id
            ),
            safe_payload={"status": "completed"},
            resume_state=phase_input.resume_state,
        )


async def _concurrent_resume() -> dict[str, object]:
    _repository, store = _services("inv-race", "inv-hook")
    hook_run, _attempt = _interrupted_run(store, "inv-hook", "run-resume-hook")
    hook_coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=_CompleteExecutor(),
        fault_injector=DeterministicFaultInjector({"concurrent_resume": 1}),
    )
    try:
        await hook_coordinator.resume(hook_run.id, owner="acceptance-hook")
    except RuntimeInjectedFault:
        pass
    else:
        raise AssertionError("concurrent resume hook did not fire")
    assert store.get_run(hook_run.id).status == RuntimeRunStatus.INTERRUPTED
    assert len(store.list_attempts(hook_run.id)) == 1
    await hook_coordinator.shutdown()

    run, _attempt = _interrupted_run(store, "inv-race", "run-resume-race")
    coordinators = [
        RuntimeCoordinator(
            store=store,
            writer=RuntimeWriter(store),
            phase_executor=_CompleteExecutor(),
        )
        for _index in range(2)
    ]
    results = await asyncio.gather(
        coordinators[0].resume(run.id, owner="acceptance-b"),
        coordinators[1].resume(run.id, owner="acceptance-c"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RuntimeRun) for item in results) == 1
    assert sum(isinstance(item, RuntimeConflict) for item in results) == 1
    assert store.get_run(run.id).status == RuntimeRunStatus.COMPLETED
    assert [attempt.status for attempt in store.list_attempts(run.id)] == [
        RuntimeAttemptStatus.INTERRUPTED,
        RuntimeAttemptStatus.COMPLETED,
    ]
    events = _assert_sequence(store, run.id)
    for coordinator in coordinators:
        await coordinator.shutdown()
    return {
        "winner_count": 1,
        "conflict_count": 1,
        "run_status": "completed",
        "attempt_count": 2,
        "event_count": len(events),
        "lease_owner": None,
    }


async def _checkpoint_tamper() -> dict[str, object]:
    rejected = 0
    replay_invalid = 0
    variants = {
        "digest": None,
        "evidence": "completed_evidence_ids",
        "finding": "completed_finding_ids",
        "review": "completed_review_ids",
        "report": "completed_report_ids",
        "tool": "successful_tool_keys",
        "checkpoint_owner": None,
    }
    for index, (variant, reference_field) in enumerate(variants.items()):
        repository = InMemoryInvestigationRepository()
        result = await DiagnosisPhaseExecutor(_orchestrator(repository)).execute(
            load_incident_case("deployment_regression"),
            InvestigationStrategy.FIXED,
        )
        record = result.record
        store = InMemoryRuntimeStore(repository)
        run = _run(store, record.id, f"run-tamper-{index}")
        leased, attempt = store.acquire_lease_and_create_attempt(
            run.id,
            attempt=RuntimeAttempt(
                run_id=run.id,
                attempt_number=1,
                status=RuntimeAttemptStatus.RUNNING,
            ),
            owner="acceptance-a",
            expected_status=RuntimeRunStatus.CREATED,
        )
        review = repository.get_coordination_review(record.id)
        tool_key = f"checkpoint-tool-key-{index}"
        repository.save_tool_calls(
            record.id,
            [
                ToolCallRecord(
                    task_id=f"checkpoint-task-{index}",
                    agent_name=AgentName.LOG,
                    tool_name="read_logs",
                    status=ToolCallStatus.SUCCESS,
                    runtime_run_id=run.id,
                    idempotency_key=tool_key,
                )
            ],
        )
        state = RuntimeResumeState(
            completed_evidence_ids=[item.id for item in record.evidence],
            completed_finding_ids=[
                item.id for item in repository.list_agent_findings(record.id)
            ],
            completed_review_ids=[review.id] if review is not None else [],
            completed_report_ids=[record.report.id] if record.report else [],
            successful_tool_keys=[tool_key],
        )
        checkpoint = store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="acceptance-a",
                lease_version=leased.lease_version,
                phase=RuntimePhase.INTAKE,
                business_mutation=BusinessMutation(investigation_id=record.id),
                safe_payload={"status": "completed"},
                resume_state=state,
            )
        )
        store.transition_run(
            run.id,
            expected=RuntimeRunStatus.RUNNING,
            target=RuntimeRunStatus.INTERRUPTED,
            owner="acceptance-a",
            lease_version=leased.lease_version,
        )
        update: dict[str, object]
        if variant == "digest":
            update = {"state_digest": "f" * 64}
        elif variant == "checkpoint_owner":
            update = {"attempt_id": "missing-attempt"}
        else:
            state_values = checkpoint.resume_state.model_dump(mode="python")
            references = list(state_values[reference_field])
            assert references
            references[0] = f"missing-{variant}"
            state_values[reference_field] = references
            tampered_state = RuntimeResumeState.model_validate(state_values)
            update = {
                "resume_state": tampered_state,
                "state_digest": checkpoint_digest(
                    run_id=checkpoint.run_id,
                    attempt_id=checkpoint.attempt_id,
                    completed_phase=checkpoint.completed_phase,
                    resume_state=tampered_state,
                ),
            }
        store._checkpoints[checkpoint.id] = checkpoint.model_copy(update=update)

        class RejectExecution:
            calls = 0

            async def execute_phase(self, _phase_input):
                self.calls += 1
                raise AssertionError("tampered checkpoint advanced execution")

        executor = RejectExecution()
        coordinator = RuntimeCoordinator(
            store=store,
            writer=RuntimeWriter(store),
            phase_executor=executor,
        )
        try:
            await coordinator.resume(run.id, owner="acceptance-b")
        except (RuntimeConflict, RuntimeNotFound):
            rejected += 1
        else:
            raise AssertionError("tampered checkpoint was accepted")
        assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
        assert len(store.list_attempts(run.id)) == 1
        assert executor.calls == 0
        replay = ReplayService(ReplayDependencies(store=store)).replay(run.id)
        assert replay.valid is False
        assert replay.external_call_count == 0
        replay_invalid += 1
        await coordinator.shutdown()
    return {
        "tamper_rejected_count": rejected,
        "tamper_variant_count": len(variants),
        "replay_invalid_count": replay_invalid,
        "run_status": "interrupted",
        "external_call_count": 0,
    }


class _RaceStore:
    def __init__(self, hub: EventHub, events: list[RuntimeEvent]) -> None:
        self.hub = hub
        self.events = events
        self.raced = False

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 500):
        matching = [
            event
            for event in self.events
            if event.run_id == run_id and event.sequence > after
        ][:limit]
        if not self.raced:
            self.raced = True
            event = _stream_event(run_id, len(self.events) + 1)
            self.events.append(event)
            self.hub.publish([event])
            matching.append(event)
        return matching


def _stream_event(run_id: str, sequence: int) -> RuntimeEvent:
    return RuntimeEvent(
        id=f"event-{run_id}-{sequence}",
        run_id=run_id,
        attempt_id=f"attempt-{run_id}",
        sequence=sequence,
        event_type=RuntimeEventType.RUN_STARTED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"status": "running"},
        occurred_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


async def _sse_reconnect() -> dict[str, object]:
    from backend.api.runtime_runs import format_sse_event

    injected = EventHub(
        fault_injector=DeterministicFaultInjector({"sse_reconnect": 1})
    )
    injected_stream = injected.stream(
        _RaceStore(injected, [_stream_event("run-hook", 1)]),
        "run-hook",
        after=1,
        heartbeat_seconds=1,
    )
    try:
        await anext(injected_stream)
    except RuntimeInjectedFault:
        pass
    else:
        raise AssertionError("SSE reconnect hook did not fire")

    hub = EventHub(max_queue_size=8)
    store = _RaceStore(
        hub,
        [_stream_event("run-sse", 1), _stream_event("run-sse", 2)],
    )
    stream = hub.stream(store, "run-sse", after=1, heartbeat_seconds=1)
    received = [await anext(stream), await anext(stream)]
    await stream.aclose()
    sequences = [event.sequence for event in received]
    assert sequences == [2, 3]

    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-sse-real",
            event=_event("inv-sse-real"),
            runtime_available=True,
        )
    )
    real_hub = EventHub(max_queue_size=8)
    real_store = InMemoryRuntimeStore(
        repository, event_publisher=real_hub.publish
    )
    run = _run(real_store, "inv-sse-real", "run-sse-real")
    leased, attempt = real_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="acceptance-sse",
        expected_status=RuntimeRunStatus.CREATED,
    )
    writer = RuntimeWriter(real_store)
    await writer.start()

    async def append(event_type: RuntimeEventType) -> None:
        await writer.submit_event(
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="acceptance-sse",
                lease_version=leased.lease_version,
                event_type=event_type,
                actor_type=RuntimeActorType.RUNTIME,
                safe_payload={"status": "running"},
            )
        )

    await append(RuntimeEventType.RUN_STARTED)
    real_stream = real_hub.stream(
        real_store, run.id, after=0, heartbeat_seconds=1
    )
    first = await anext(real_stream)
    await append(RuntimeEventType.ATTEMPT_STARTED)
    second = await anext(real_stream)
    await real_stream.aclose()
    assert [first.sequence, second.sequence] == [1, 2]
    reconnect = real_hub.stream(
        real_store, run.id, after=1, heartbeat_seconds=1
    )
    assert (await anext(reconnect)).sequence == 2
    await reconnect.aclose()

    slow = real_hub.subscribe(run.id)
    for _index in range(9):
        await append(RuntimeEventType.RUN_STARTED)
    await asyncio.sleep(0)
    assert slow.closed is True
    await append(RuntimeEventType.RUN_STARTED)
    latest = real_store.list_events(run.id)[-1].sequence

    async def disconnected() -> bool:
        return True

    disconnected_stream = real_hub.stream(
        real_store,
        run.id,
        after=latest,
        heartbeat_seconds=1,
        is_disconnected=disconnected,
    )
    try:
        await anext(disconnected_stream)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("disconnected SSE client remained subscribed")
    assert real_store.get_run(run.id).status == RuntimeRunStatus.RUNNING
    assert real_store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.RUNNING
    real_events = _assert_sequence(real_store, run.id)
    sse_frames = [format_sse_event(event) for event in real_events]
    await writer.shutdown()
    return {
        "last_event_id": 1,
        "received_sequences": sequences,
        "missing_count": 0,
        "duplicate_count": 0,
        "real_committed_event_count": len(real_events),
        "slow_client_isolated": True,
        "disconnect_did_not_cancel": True,
        "run_status": "running",
        "serialized_sse_frame_count": len(sse_frames),
        "_privacy_surfaces": sse_frames,
    }


async def _parallel_session_failure() -> dict[str, object]:
    investigation_ids = tuple(f"inv-session-{index}" for index in range(4))
    repository, store = _services(*investigation_ids)
    runs = [
        store.create_run(
            RuntimeRun(
                id=f"run-session-{index}",
                investigation_id=investigation_id,
                run_kind=RuntimeRunKind.LIVE,
                strategy=InvestigationStrategy.FIXED,
                run_reason=RuntimeRunReason.INITIAL,
                tool_budget=10 + index,
                token_budget=None,
            )
        )
        for index, investigation_id in enumerate(investigation_ids)
    ]
    lock = ThreadLock()
    release = ThreadEvent()
    tracker = {"active": 0, "peak": 0, "started": []}

    class BlockingProvider:
        provider = EvidenceProvider.LOG

        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def collect(self, _event):
            with lock:
                tracker["active"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["active"])
                tracker["started"].append(self.run_id)
            release.wait(5)
            with lock:
                tracker["active"] -= 1
            return ProviderResult(provider=self.provider)

    class FailingAnalyzer(RcaAnalyzer):
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("isolated session failure")

    def executor(run_id: str, *, fail: bool) -> DiagnosisPhaseExecutor:
        providers = ProviderRegistry([BlockingProvider(run_id)])
        orchestrator = DiagnosisOrchestrator(
            repository=repository,
            providers=providers,
            analyzer=FailingAnalyzer() if fail else RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
        )
        return DiagnosisPhaseExecutor(orchestrator, max_parallel_steps_per_run=3)

    coordinators = {
        run.id: RuntimeCoordinator(
            store=store,
            writer=RuntimeWriter(store),
            phase_executor=executor(run.id, fail=index == 2),
        )
        for index, run in enumerate(runs)
    }
    manager = RuntimeManager(
        coordinator_factory=coordinators.__getitem__,
        max_concurrent_runs=4,
    )
    tasks = [await manager.start(run.id) for run in runs]
    while True:
        with lock:
            if len(tracker["started"]) >= 4:
                break
        await asyncio.sleep(0)
    assert tracker["peak"] == 4
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(item, RuntimeError) for item in results) == 1, [
        type(item).__name__ for item in results
    ]
    statuses = [store.get_run(run.id).status for run in runs]
    assert statuses.count(RuntimeRunStatus.COMPLETED) == 3
    assert statuses.count(RuntimeRunStatus.FAILED) == 1
    for index, run in enumerate(runs):
        record = repository.get(run.investigation_id)
        assert record.id == investigation_ids[index]
        assert record.event.title.endswith(investigation_ids[index])
        persisted = store.get_run(run.id)
        assert persisted.tool_budget == 10 + index
        assert persisted.token_budget is None
        assert all(event.run_id == run.id for event in _assert_sequence(store, run.id))
        attempts = store.list_attempts(run.id)
        assert len(attempts) == 1
        assert attempts[0].status == (
            RuntimeAttemptStatus.FAILED
            if index == 2
            else RuntimeAttemptStatus.COMPLETED
        )
    for coordinator in coordinators.values():
        await coordinator.shutdown()

    step_record = repository.save(
        InvestigationRecord(id="inv-session-steps", event=_event("inv-session-steps"))
    )
    step_run = _run(store, step_record.id, "run-session-steps")
    step_lock = ThreadLock()
    step_release = ThreadEvent()
    step_tracker = {"active": 0, "peak": 0}

    class StepProvider:
        provider = EvidenceProvider.LOG

        def collect(self, _event):
            with step_lock:
                step_tracker["active"] += 1
                step_tracker["peak"] = max(
                    step_tracker["peak"], step_tracker["active"]
                )
            step_release.wait(5)
            with step_lock:
                step_tracker["active"] -= 1
            return ProviderResult(provider=self.provider)

    step_providers = ProviderRegistry([StepProvider() for _index in range(6)])
    step_orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=step_providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(step_providers),
    )
    step_coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(
            step_orchestrator, max_parallel_steps_per_run=3
        ),
    )
    aggregate = asyncio.create_task(
        step_coordinator.execute(step_run.id, owner="acceptance-steps")
    )
    while True:
        with step_lock:
            if step_tracker["peak"] == 3:
                break
        await asyncio.sleep(0)
    assert step_tracker["peak"] == 3
    step_release.set()
    await aggregate
    await step_coordinator.shutdown()
    await manager.shutdown()
    return {
        "session_peak": tracker["peak"],
        "session_success_count": 3,
        "session_failure_count": 1,
        "parallel_step_peak": step_tracker["peak"],
        "isolated_step_failure_count": 0,
        "cross_session_contamination_count": 0,
    }


async def _cancel_parallel_specialists() -> dict[str, object]:
    repository, store = _services("inv-cancel", "inv-healthy")
    cancel_run = _run(store, "inv-cancel", "run-cancel")
    healthy_run = _run(store, "inv-healthy", "run-healthy")
    lock = ThreadLock()
    release = ThreadEvent()
    tracker = {"active": 0, "peak": 0}

    class CancelProvider:
        provider = EvidenceProvider.LOG

        def collect(self, _event):
            with lock:
                tracker["active"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["active"])
            release.wait(5)
            with lock:
                tracker["active"] -= 1
            return ProviderResult(provider=self.provider)

    providers = ProviderRegistry([CancelProvider() for _index in range(3)])
    cancel_orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )

    cancel_coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(
            cancel_orchestrator, max_parallel_steps_per_run=3
        ),
        fault_injector=DeterministicFaultInjector(
            {"cancel_parallel_specialists": 1}
        ),
    )
    healthy_coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(_orchestrator(repository)),
    )
    cancel_task = asyncio.create_task(
        cancel_coordinator.execute(cancel_run.id, owner="acceptance-cancel")
    )
    while True:
        with lock:
            if tracker["peak"] == 3:
                break
        await asyncio.sleep(0)
    healthy_task = asyncio.create_task(
        healthy_coordinator.execute(healthy_run.id, owner="acceptance-healthy")
    )
    try:
        await cancel_coordinator.request_cancel(cancel_run.id)
    except RuntimeInjectedFault:
        pass
    else:
        raise AssertionError("parallel cancellation hook did not fire")
    assert store.get_run(cancel_run.id).status == RuntimeRunStatus.CANCELLING
    release.set()
    cancelled, healthy = await asyncio.gather(cancel_task, healthy_task)
    assert cancelled.status == RuntimeRunStatus.CANCELLED
    assert healthy.status == RuntimeRunStatus.COMPLETED
    assert repository.get("inv-cancel").failure_reason is None
    assert repository.get("inv-healthy").failure_reason is None
    assert [
        item.completed_phase for item in store.list_checkpoints(cancel_run.id)
    ] == [RuntimePhase.INTAKE]
    cancel_events = _assert_sequence(store, cancel_run.id)
    assert RuntimeEventType.RUN_CANCELLED in {event.event_type for event in cancel_events}
    assert RuntimePhase.EVIDENCE_COLLECTION not in {
        event.phase
        for event in cancel_events
        if event.event_type == RuntimeEventType.PHASE_COMPLETED
    }
    assert len(store.list_checkpoints(healthy_run.id)) == len(V10_PHASE_ORDER)
    await cancel_coordinator.shutdown()
    await healthy_coordinator.shutdown()
    return {
        "cancel_observed": True,
        "run_status": "cancelled",
        "attempt_status": "cancelled",
        "late_result_count": 0,
        "external_mutation_count": 0,
        "healthy_session_status": "completed",
    }


class _SlowExporter(SpanExporter):
    def export(self, spans):
        time.sleep(0.01)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        return None


async def _otel_unavailable() -> dict[str, object]:
    assert RuntimeTelemetry.disabled().enabled is False
    unavailable = RuntimeTelemetry.from_settings(
        OpenTelemetrySettings(
            enabled=True,
            endpoint="http://127.0.0.1:4318/v1/traces",
        ),
        fault_injector=DeterministicFaultInjector({"otel_unavailable": 1}),
    )
    assert unavailable.enabled is False
    repository, store = _services("inv-otel")
    run = _run(store, "inv-otel", "run-otel")
    exporter = InMemorySpanExporter()
    linked = RuntimeTelemetry.for_exporter(exporter)
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=_CompleteExecutor(),
        telemetry=linked,
    )
    completed = await coordinator.execute(run.id, owner="acceptance-otel")
    assert completed.status == RuntimeRunStatus.COMPLETED
    await coordinator.shutdown()
    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name == "runtime.attempt"]
    phases = [span for span in spans if span.name == "runtime.phase"]
    assert len(roots) == 1
    assert len(phases) == len(V10_PHASE_ORDER)
    assert all(span.context.trace_id == roots[0].context.trace_id for span in phases)
    assert all(span.parent.span_id == roots[0].context.span_id for span in phases)
    assert store.get_run(run.id).status == RuntimeRunStatus.COMPLETED
    slow = RuntimeTelemetry.for_exporter(_SlowExporter())
    with slow.span(
        "runtime.phase",
        {"run_id": "run-otel", "phase": "intake", "status": "completed"},
    ):
        pass
    assert slow.force_flush(timeout_seconds=0.1) is True
    assert slow.shutdown(timeout_seconds=0.1) is True
    return {
        "default_off": True,
        "unavailable_falls_back_to_noop": True,
        "slow_collector_isolated": True,
        "execution_failure_count": 0,
        "linked_attempt_count": len(roots),
        "linked_phase_count": len(phases),
        "run_status": "completed",
        "_privacy_surfaces": [
            json.dumps(
                RuntimeTelemetry.safe_attributes(
                    {
                        "run_id": "run-otel",
                        "phase": "intake",
                        "status": "completed",
                    }
                )
            )
        ],
    }


async def _replay_external_call() -> dict[str, object]:
    with _temporary_sqlite_engine("diagops-acceptance-replay-") as engine:
        repository = SQLiteInvestigationRepository(engine)
        store = SQLiteRuntimeStore(engine, repository)

        async def create_source(index: int):
            investigation_id = f"inv-replay-{index}"
            repository.save(
                InvestigationRecord(
                    id=investigation_id,
                    event=_event(investigation_id),
                    runtime_available=True,
                )
            )
            run = _run(store, investigation_id, f"run-replay-{index}")
            coordinator = RuntimeCoordinator(
                store=store,
                writer=RuntimeWriter(store),
                phase_executor=_CompleteExecutor(),
            )
            try:
                completed = await coordinator.execute(
                    run.id, owner=f"acceptance-replay-{index}"
                )
                assert completed.status == RuntimeRunStatus.COMPLETED
                return completed
            finally:
                await coordinator.shutdown()

        source = await create_source(0)
        dependencies = ReplayDependencies(store=store)
        rejected = ReplayService(
            dependencies,
            fault_injector=DeterministicFaultInjector({"replay_external_call": 1}),
        ).replay(source.id)
        assert rejected.valid is False
        assert rejected.external_call_count == 1
        clean = ReplayService(dependencies).replay(source.id)
        assert clean.valid is True
        assert clean.external_call_count == 0
        assert sum(
            int(event.safe_payload.get("input_tokens", 0))
            + int(event.safe_payload.get("output_tokens", 0))
            for event in store.list_events(source.id)
        ) == 0
        diff_service = RuntimeDiffService(store=store)
        first_diff = canonical_diff_json(diff_service.compare(source.id, source.id))
        assert first_diff == canonical_diff_json(
            diff_service.compare(source.id, source.id)
        )

        corruption_codes = {}
        expected_codes = {
            "gap": "event_sequence_gap",
            "illegal_transition": "illegal_phase_transition",
            "invalid_reference": "bad_reference",
            "checkpoint_tamper": "checkpoint_digest_mismatch",
        }
        for index, kind in enumerate(
            ("gap", "illegal_transition", "invalid_reference", "checkpoint_tamper"),
            start=1,
        ):
            corrupted_source = await create_source(index)
            events = store.list_events(corrupted_source.id)
            with engine.begin() as connection:
                if kind == "gap":
                    connection.execute(
                        runtime_events.update()
                        .where(runtime_events.c.id == events[2].id)
                        .values(sequence=99)
                    )
                elif kind == "illegal_transition":
                    phase_completed = next(
                        event
                        for event in events
                        if event.event_type == RuntimeEventType.PHASE_COMPLETED
                    )
                    connection.execute(
                        runtime_events.update()
                        .where(runtime_events.c.id == phase_completed.id)
                        .values(event_type=RuntimeEventType.PHASE_STARTED.value)
                    )
                elif kind == "invalid_reference":
                    connection.execute(
                        runtime_events.update()
                        .where(runtime_events.c.id == events[2].id)
                        .values(evidence_ids=["missing-evidence"])
                    )
                else:
                    checkpoint = store.list_checkpoints(corrupted_source.id)[0]
                    connection.execute(
                        runtime_checkpoints.update()
                        .where(runtime_checkpoints.c.id == checkpoint.id)
                        .values(state_digest="f" * 64)
                    )
            report = ReplayService(dependencies).replay(corrupted_source.id)
            assert report.valid is False
            assert report.external_call_count == 0
            expected_code = expected_codes[kind]
            assert expected_code in report.validation_errors
            corruption_codes[kind] = expected_code
        frozen_snapshot = json.dumps(
            store.get_frozen_business_projection(source.id),
            ensure_ascii=False,
        )
        return {
            "external_attempt_detected": True,
            "clean_external_call_count": 0,
            "model_usage_count": 0,
            "corruption_variant_count": 4,
            "corruption_detected_count": 4,
            "corruption_codes": corruption_codes,
            "diff_stable": True,
            "replay_report_id": clean.id,
            "_privacy_surfaces": [
                clean.model_dump_json(),
                first_diff,
                frozen_snapshot,
            ],
        }


async def _unsafe_event_payload() -> dict[str, object]:
    _repository, store = _services("inv-unsafe")
    run = _run(store, "inv-unsafe", "run-unsafe")
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="acceptance-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    rejected_payloads = (
        (RuntimeEventType.EVIDENCE_REJECTED, {"evidence_body": "sensitive"}),
        (RuntimeEventType.EVIDENCE_REJECTED, {"provider_response": "sensitive"}),
        (RuntimeEventType.TOOL_FAILED, {"tool_result_text": "sensitive"}),
    )
    for event_type, payload in rejected_payloads:
        command = RuntimeEventCommand(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="acceptance-a",
            lease_version=leased.lease_version,
            event_type=event_type,
            actor_type=RuntimeActorType.PROVIDER,
            safe_payload=payload,
        )
        try:
            store.append_event_command(command)
        except ValidationError:
            pass
        else:
            raise AssertionError("unstructured runtime content was persisted")
    assert store.list_events(run.id) == []
    store.fault_injector = DeterministicFaultInjector({"unsafe_event_payload": 1})
    safe_command = RuntimeEventCommand(
        run_id=run.id,
        attempt_id=attempt.id,
        lease_owner="acceptance-a",
        lease_version=leased.lease_version,
        event_type=RuntimeEventType.EVIDENCE_REJECTED,
        actor_type=RuntimeActorType.PROVIDER,
        safe_payload={"status": "failed", "safe_code": "payload_rejected"},
    )
    try:
        store.append_event_command(safe_command)
    except RuntimeInjectedFault:
        pass
    else:
        raise AssertionError("unsafe payload hook did not fire")
    assert store.list_events(run.id) == []
    assert store.get_run(run.id).status == RuntimeRunStatus.RUNNING
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.RUNNING
    return {
        "sensitive_content_rejected": True,
        "unstructured_content_rejected": True,
        "rejected_content_kind_count": len(rejected_payloads),
        "persisted_event_count": 0,
        "run_status": "running",
        "attempt_status": "running",
        "lease_owner": "acceptance-a",
    }


_SCENARIOS: dict[str, Callable[[], Awaitable[dict[str, object]]]] = {
    "before_phase_start": lambda: _boundary_scenario("before_phase_start"),
    "provider_before_commit": lambda: _boundary_scenario("provider_before_commit"),
    "tool_after_commit_before_checkpoint": _tool_after_commit_before_checkpoint,
    "model_after_send": lambda: _boundary_scenario("model_after_send"),
    "persistence_mid_transaction": _persistence_mid_transaction,
    "lease_lost": lambda: _boundary_scenario("lease_lost"),
    "concurrent_resume": _concurrent_resume,
    "sse_reconnect": _sse_reconnect,
    "checkpoint_tamper": _checkpoint_tamper,
    "parallel_session_failure": _parallel_session_failure,
    "cancel_parallel_specialists": _cancel_parallel_specialists,
    "otel_unavailable": _otel_unavailable,
    "replay_external_call": _replay_external_call,
    "unsafe_event_payload": _unsafe_event_payload,
}


def _orchestrator(repository=None) -> DiagnosisOrchestrator:
    repository = repository or InMemoryInvestigationRepository()
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )


def _percent(current: float, baseline: float) -> float:
    return round(((current - baseline) / baseline) * 100, 3) if baseline else 0.0


def _latency(
    values: list[float], *, iterations: int, warmup_iterations: int
) -> dict[str, object]:
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "p50_wall_ms": round(median(ordered), 3),
        "p95_wall_ms": round(ordered[p95_index], 3),
        "raw_wall_ms": [round(value, 3) for value in values],
        "sample_count": len(values),
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "deterministic_case_count": len(_CASES),
    }


def _performance_container(enabled: bool, storage_url: str):
    from backend.config.settings import AppSettings, RuntimeSettings, StorageSettings
    from backend.services.container import AppContainer

    settings = AppSettings(
        storage=StorageSettings(url=storage_url),
        runtime=RuntimeSettings(enabled=enabled),
    )
    settings.providers.log_file.enabled = False
    settings.providers.deployment_file.enabled = False
    settings.providers.service_catalog.enabled = False
    return AppContainer(settings)


def _measure_sync_mode(
    cases: tuple[str, ...], *, iterations: int, warmup_iterations: int
) -> list[float]:
    with tempfile.TemporaryDirectory(prefix="diagops-sync-performance-") as temp_dir:
        storage_url = f"sqlite:///{Path(temp_dir) / 'sync.db'}"
        container = _performance_container(False, storage_url)
        values: list[float] = []
        try:
            for iteration in range(warmup_iterations + iterations):
                for case_id in cases:
                    started = time.perf_counter()
                    container.orchestrator.run(
                        load_incident_case(case_id), InvestigationStrategy.FIXED
                    )
                    if iteration >= warmup_iterations:
                        values.append((time.perf_counter() - started) * 1000)
        finally:
            container.close()
        return values


async def _measure_disabled_mode(
    cases: tuple[str, ...],
    *,
    iterations: int,
    warmup_iterations: int,
    container_factory: Callable[[], object] | None = None,
) -> list[float]:
    owns_container = container_factory is None
    with tempfile.TemporaryDirectory(prefix="diagops-disabled-performance-") as temp_dir:
        container = (
            _performance_container(
                False, f"sqlite:///{Path(temp_dir) / 'disabled.db'}"
            )
            if owns_container
            else container_factory()
        )
        values: list[float] = []
        try:
            for iteration in range(warmup_iterations + iterations):
                for case_id in cases:
                    started = time.perf_counter()
                    await container.run_investigation(
                        load_incident_case(case_id),
                        strategy=InvestigationStrategy.FIXED,
                    )
                    if iteration >= warmup_iterations:
                        values.append((time.perf_counter() - started) * 1000)
        finally:
            if owns_container:
                container.close()
        return values


async def _measure_enabled_mode(
    cases: tuple[str, ...], *, iterations: int, warmup_iterations: int
) -> list[float]:
    with tempfile.TemporaryDirectory(prefix="diagops-enabled-performance-") as temp_dir:
        container = _performance_container(
            True, f"sqlite:///{Path(temp_dir) / 'enabled.db'}"
        )
        values: list[float] = []
        try:
            for iteration in range(warmup_iterations + iterations):
                for case_id in cases:
                    started = time.perf_counter()
                    await container.run_investigation(
                        load_incident_case(case_id),
                        strategy=InvestigationStrategy.FIXED,
                    )
                    if iteration >= warmup_iterations:
                        values.append((time.perf_counter() - started) * 1000)
        finally:
            await container.shutdown()
        return values


async def _measure_performance() -> tuple[dict, dict, dict]:
    iterations = 2
    warmup_iterations = 1
    sync_values = await asyncio.to_thread(
        _measure_sync_mode,
        _CASES,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    disabled_values = await _measure_disabled_mode(
        _CASES,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    enabled_values = await _measure_enabled_mode(
        _CASES,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )

    with tempfile.TemporaryDirectory(prefix="diagops-runtime-acceptance-") as temp_dir:
        database_path = Path(temp_dir) / "runtime.db"
        engine = create_db_engine(f"sqlite:///{database_path}")
        initialize_database(engine)
        initial_bytes = database_path.stat().st_size
        repository = SQLiteInvestigationRepository(engine)
        store = SQLiteRuntimeStore(engine, repository)
        run_ids = []
        for index, case_id in enumerate(_CASES):
            record = repository.save(
                InvestigationRecord(
                    id=f"acceptance-performance-{index}",
                    event=load_incident_case(case_id),
                    runtime_available=True,
                )
            )
            run = store.create_run(
                RuntimeRun(
                    id=f"acceptance-performance-run-{index}",
                    investigation_id=record.id,
                    run_kind=RuntimeRunKind.LIVE,
                    strategy=InvestigationStrategy.FIXED,
                    run_reason=RuntimeRunReason.INITIAL,
                    prompt_version="v8.2-compatible",
                )
            )
            run_ids.append(run.id)
            coordinator = RuntimeCoordinator(
                store=store,
                writer=RuntimeWriter(store),
                phase_executor=DiagnosisPhaseExecutor(_orchestrator(repository)),
                telemetry=RuntimeTelemetry.disabled(),
            )
            completed = await coordinator.execute(
                run.id, owner=f"acceptance-performance-{index}"
            )
            assert completed.status == RuntimeRunStatus.COMPLETED
            await coordinator.shutdown()
        final_bytes = database_path.stat().st_size
        event_counts = [len(store.list_events(run_id)) for run_id in run_ids]
        checkpoint_counts = [len(store.list_checkpoints(run_id)) for run_id in run_ids]
        engine.dispose()

    sync_latency = _latency(
        sync_values,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    disabled_latency = _latency(
        disabled_values,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    enabled_latency = _latency(
        enabled_values,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    sync_latency["storage"] = "sqlite"
    disabled_latency["storage"] = "sqlite"
    enabled_latency["storage"] = "sqlite"
    latency = {
        "v8_2_sync": sync_latency,
        "runtime_disabled": disabled_latency,
        "runtime_enabled": enabled_latency,
        "comparison_percentages": {
            "runtime_disabled_vs_v8_2_sync_p50": _percent(
                disabled_latency["p50_wall_ms"], sync_latency["p50_wall_ms"]
            ),
            "runtime_enabled_vs_v8_2_sync_p50": _percent(
                enabled_latency["p50_wall_ms"], sync_latency["p50_wall_ms"]
            ),
            "runtime_enabled_vs_runtime_disabled_p50": _percent(
                enabled_latency["p50_wall_ms"], disabled_latency["p50_wall_ms"]
            ),
        },
    }
    database_growth = {
        "sqlite_bytes_per_run": round((final_bytes - initial_bytes) / len(_CASES), 3),
        "event_count_per_run": round(sum(event_counts) / len(event_counts), 3),
        "checkpoint_count_per_run": round(
            sum(checkpoint_counts) / len(checkpoint_counts), 3
        ),
        "run_count": len(_CASES),
    }

    def telemetry_measure(enabled: bool) -> float:
        telemetry = (
            RuntimeTelemetry.for_exporter(_CountingExporter())
            if enabled
            else RuntimeTelemetry.disabled()
        )
        started = time.perf_counter()
        for index in range(100):
            with telemetry.span(
                "runtime.phase",
                {"run_id": "performance", "phase": "intake", "duration_ms": index},
            ):
                pass
        elapsed = (time.perf_counter() - started) * 1000
        telemetry.shutdown(timeout_seconds=1)
        return elapsed

    disabled_ms = await asyncio.to_thread(telemetry_measure, False)
    enabled_ms = await asyncio.to_thread(telemetry_measure, True)
    otel = {
        "disabled_wall_ms": round(disabled_ms, 3),
        "enabled_wall_ms": round(enabled_ms, 3),
        "percent": float(_percent(enabled_ms, disabled_ms)),
    }
    return latency, database_growth, otel


class _CountingExporter(SpanExporter):
    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        return None


def _scan_content(content: str) -> list[str]:
    findings = []
    if _PROHIBITED_FIELD.search(content):
        findings.append("prohibited_field")
    if _PROHIBITED_VALUE.search(content):
        findings.append("prohibited_value")
    for match in _URL_VALUE.finditer(content):
        parsed = urlsplit(match.group(0))
        if parsed.username is not None or parsed.password is not None:
            findings.append("url_userinfo")
    return findings


def _normalized_key_segments(key: str) -> tuple[set[str], str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    segments = {
        item.lower() for item in re.split(r"[^a-zA-Z0-9]+", expanded) if item
    }
    return segments, "".join(sorted(segments))


def _prohibited_key(key: str) -> bool:
    segments, compact = _normalized_key_segments(key)
    if segments & {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "prompt",
        "reasoning",
        "password",
        "secret",
        "token",
    }:
        return True
    if {"api", "key"} <= segments or compact in {"apikey", "chainofthought", "cot"}:
        return True
    if "evidence" in segments and segments & {"body", "content", "text"}:
        return True
    if "log" in segments and segments & {"body", "content", "text"}:
        return True
    if {"model", "output"} <= segments:
        return True
    if "raw" in segments and segments & {"output", "payload"}:
        return True
    if "payload" in segments and segments & {"request", "response"}:
        return True
    if "tool" in segments and segments & {"output", "result"}:
        return True
    return "provider" in segments and bool(
        segments & {"payload", "request", "response"}
    )


def _scan_value(value: object) -> list[str]:
    findings: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or _prohibited_key(key):
                    findings.add("prohibited_field")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            findings.update(_scan_content(item))

    visit(value)
    return sorted(findings)


def validate_acceptance_artifact(artifact: dict) -> None:
    scenario_results = artifact.get("scenario_results", [])
    actual = {
        item.get("name")
        for item in scenario_results
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    missing = REQUIRED_SCENARIOS - actual
    if missing:
        raise RuntimeError(
            f"missing required acceptance scenarios: {', '.join(sorted(missing))}"
        )
    if actual - REQUIRED_SCENARIOS:
        raise RuntimeError("unexpected runtime acceptance scenarios")
    if any(item.get("status") != "passed" for item in scenario_results):
        raise RuntimeError("runtime acceptance scenario failed")
    findings = _scan_value(artifact)
    if findings:
        raise RuntimeError("prohibited runtime acceptance content")
    if artifact.get("privacy_scan") != {
        "status": "passed",
        "prohibited_markers_found": [],
    }:
        raise RuntimeError("prohibited runtime acceptance content")


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _candidate_state(project_root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        check=True,
    ).stdout
    tracked_diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=project_root,
        capture_output=True,
        check=True,
    ).stdout
    untracked = sorted(
        entry[3:].decode("utf-8", errors="surrogateescape")
        for entry in status.split(b"\0")
        if entry.startswith(b"?? ")
    )
    digest = hashlib.sha256()
    digest.update(b"tracked-diff\0")
    digest.update(tracked_diff)
    for relative_name in untracked:
        candidate = project_root / relative_name
        if not candidate.is_file():
            continue
        digest.update(b"\0untracked-name\0")
        digest.update(relative_name.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0untracked-content\0")
        digest.update(candidate.read_bytes())
    return bool(status), digest.hexdigest()


async def run_acceptance(
    *,
    output_root: Path = PROJECT_ROOT / "output" / "runtime-acceptance",
    git_commit: str | None = None,
    scenario_timeout_seconds: float = 10,
) -> Path:
    if scenario_timeout_seconds <= 0:
        raise ValueError("scenario_timeout_seconds must be positive")
    run_id = f"runtime-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
    run_dir = output_root / run_id
    scenario_results = []
    privacy_surfaces: list[str] = []
    recovery_wall_ms = 0.0

    class RuntimeLogCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            privacy_surfaces.append(self.format(record))

    runtime_logger = logging.getLogger("backend.runtime")
    log_capture = RuntimeLogCapture()
    runtime_logger.addHandler(log_capture)
    try:
        for name in sorted(REQUIRED_SCENARIOS):
            started = time.perf_counter()
            observations: dict[str, object]
            try:
                observations = await asyncio.wait_for(
                    _SCENARIOS[name](), timeout=scenario_timeout_seconds
                )
            except Exception as exc:
                observations = {"error_type": type(exc).__name__}
                status = "failed"
            else:
                status = "passed"
                privacy_surfaces.extend(
                    str(item) for item in observations.pop("_privacy_surfaces", [])
                )
                recovery_wall_ms = max(
                    recovery_wall_ms,
                    float(observations.get("recovery_wall_ms", 0)),
                )
            scenario_results.append(
                {
                    "name": name,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "observations": observations,
                }
            )
        latency, database_growth, otel_overhead = await asyncio.wait_for(
            _measure_performance(), timeout=max(30, scenario_timeout_seconds * 10)
        )
    finally:
        runtime_logger.removeHandler(log_capture)
    privacy_findings = _scan_value(privacy_surfaces)
    git_dirty, candidate_diff_sha256 = _candidate_state()
    artifact = {
        "schema_version": 1,
        "run_id": run_id,
        "git_commit": git_commit or _git_commit(),
        "git_dirty": git_dirty,
        "candidate_diff_sha256": candidate_diff_sha256,
        "config": {
            "seed": SEED,
            "scenario_timeout_seconds": scenario_timeout_seconds,
            "max_concurrent_runs": 4,
            "max_parallel_steps_per_run": 3,
            "deterministic_cases": list(_CASES),
            "key_free": True,
        },
        "scenario_results": scenario_results,
        "latency": latency,
        "database_growth": database_growth,
        "otel_overhead": otel_overhead,
        "recovery_time": {"wall_ms": round(recovery_wall_ms, 3)},
        "privacy_scan": {
            "status": "passed" if not privacy_findings else "failed",
            "prohibited_markers_found": privacy_findings,
        },
    }
    validate_acceptance_artifact(artifact)
    run_dir.mkdir(parents=True, exist_ok=False)
    artifact_path = run_dir / "result.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=run_dir,
            prefix=".result-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, artifact_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return artifact_path.resolve()


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Run key-free Runtime acceptance")
    parser.add_argument("--scenario-timeout-seconds", type=float, default=10)
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    path = asyncio.run(
        run_acceptance(
            output_root=PROJECT_ROOT / "output" / "runtime-acceptance",
            scenario_timeout_seconds=args.scenario_timeout_seconds,
        )
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
