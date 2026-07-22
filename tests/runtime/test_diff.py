import json
from datetime import UTC, datetime, timedelta

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import InvestigationStrategy, ModelProvider
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.runtime.diff import RuntimeDiffService, canonical_diff_json
from backend.runtime.store import (
    InMemoryRuntimeStore,
    RuntimeTerminalCommit,
    RuntimeTerminalEvent,
)
from backend.runtime.writer import RuntimeEventCommand


def _fixture():
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-diff",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="diff",
                description="diff fixture",
                started_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )
    )
    store = InMemoryRuntimeStore(repository)
    base = datetime(2026, 7, 18, tzinfo=UTC)
    left = store.create_run(
        RuntimeRun(
            id="run-left",
            investigation_id="inv-diff",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.INITIAL,
            model_provider=ModelProvider.OPENAI,
            model_name="gpt-a",
            prompt_version="v1",
            started_at=base,
            completed_at=base + timedelta(seconds=2),
        )
    )
    right = store.create_run(
        RuntimeRun(
            id="run-right",
            investigation_id="inv-diff",
            run_kind=RuntimeRunKind.REPLAY,
            strategy=InvestigationStrategy.ADAPTIVE,
            status=RuntimeRunStatus.FAILED,
            run_reason=RuntimeRunReason.REPLAY,
            source_run_id=left.id,
            model_provider=ModelProvider.DEEPSEEK,
            model_name="deepseek-b",
            prompt_version="v2",
            started_at=base,
            completed_at=base + timedelta(seconds=3),
        )
    )
    store._events[left.id] = _events(left.id, "LogAgent", "read_logs", 10, 0.2)
    store._events[right.id] = _events(right.id, "MetricAgent", "query_metrics", 20, 0.4)
    return store, repository, left, right


def _events(run_id: str, agent: str, tool: str, tokens: int, cost: float):
    base = datetime(2026, 7, 18, tzinfo=UTC)
    definitions = [
        (RuntimeEventType.PHASE_STARTED, RuntimeActorType.PHASE, {"status": "running"}),
        (
            RuntimeEventType.AGENT_COMPLETED,
            RuntimeActorType.AGENT,
            {"status": "completed", "input_tokens": tokens, "cost": cost},
        ),
        (
            RuntimeEventType.TOOL_COMPLETED,
            RuntimeActorType.TOOL,
            {
                "status": "completed",
                "tool_name": tool,
                "normalized_inputs": {"limit": 10},
            },
        ),
        (
            RuntimeEventType.PHASE_COMPLETED,
            RuntimeActorType.PHASE,
            {"status": "completed"},
        ),
    ]
    return [
        RuntimeEvent(
            run_id=run_id,
            attempt_id=f"attempt-{run_id}",
            sequence=index,
            event_type=event_type,
            phase=RuntimePhase.SPECIALIST_ANALYSIS,
            actor_type=actor,
            actor_name=agent if actor == RuntimeActorType.AGENT else None,
            evidence_ids=[f"evidence-{run_id}"] if index == 4 else [],
            safe_payload=payload,
            occurred_at=base + timedelta(milliseconds=index * 100),
        )
        for index, (event_type, actor, payload) in enumerate(definitions, start=1)
    ]


def test_diff_is_stable_typed_and_omits_free_text() -> None:
    store, repository, left, right = _fixture()
    service = RuntimeDiffService(store=store)

    first = service.compare(left.id, right.id)
    second = service.compare(left.id, right.id)
    first_json = canonical_diff_json(first)
    second_json = canonical_diff_json(second)

    assert first_json == second_json
    assert json.loads(first_json)["run_id"] == left.id
    assert set(first.sections) >= {
        "configuration",
        "phases",
        "agents",
        "tools",
        "evidence_references",
        "causes",
        "metrics",
        "fallback_and_failure",
    }
    assert all(
        set(section.model_dump()) == {"left", "right", "changed"}
        for section in first.sections.values()
    )
    lowered = first_json.lower()
    assert "raw prompt" not in lowered
    assert "model prose" not in lowered
    assert "description" not in lowered
    assert first.sections["configuration"].changed is True
    assert first.sections["phases"].changed is False
    assert first.sections["agents"].changed is True
    assert first.sections["tools"].changed is True
    assert first.sections["evidence_references"].changed is True


def test_diff_reads_metrics_and_references_after_first_event_page(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    evidence = EvidenceItem(
        id="evidence-after-page",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="bounded fixture",
    )
    repository.save(record.model_copy(update={"evidence": [evidence]}))

    def long_run(run_id: str, tokens: int, include_reference: bool) -> RuntimeRun:
        run = runtime_store.create_run(
            RuntimeRun(
                id=run_id,
                investigation_id="inv-1",
                run_kind=RuntimeRunKind.LIVE,
                strategy=InvestigationStrategy.FIXED,
                    run_reason=RuntimeRunReason.INITIAL,
            )
        )
        attempt = RuntimeAttempt(
            id=f"attempt-{run_id}",
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        )
        run, attempt = runtime_store.acquire_lease_and_create_attempt(
            run.id,
            attempt=attempt,
            owner=run.id,
            expected_status=RuntimeRunStatus.CREATED,
        )
        for event_type in (
            RuntimeEventType.RUN_STARTED,
            RuntimeEventType.ATTEMPT_STARTED,
        ):
            runtime_store.append_event_command(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=run.id,
                    lease_version=run.lease_version,
                    event_type=event_type,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={"status": "running"},
                )
            )
        for _index in range(500):
            runtime_store.append_event_command(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=run.id,
                    lease_version=run.lease_version,
                    event_type=RuntimeEventType.EVIDENCE_REJECTED,
                    actor_type=RuntimeActorType.PROVIDER,
                    safe_payload={"status": "rejected"},
                )
            )
        runtime_store.append_event_command(
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner=run.id,
                lease_version=run.lease_version,
                event_type=RuntimeEventType.MODEL_COMPLETED,
                actor_type=RuntimeActorType.MODEL,
                safe_payload={
                    "status": "completed",
                    "input_tokens": tokens,
                    "output_tokens": 0,
                },
            )
        )
        if include_reference:
            runtime_store.append_event_command(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=run.id,
                    lease_version=run.lease_version,
                    event_type=RuntimeEventType.EVIDENCE_PERSISTED,
                    actor_type=RuntimeActorType.PROVIDER,
                    evidence_ids=(evidence.id,),
                    safe_payload={"status": "completed", "evidence_count": 1},
                )
            )
        runtime_store.commit_terminal(
            RuntimeTerminalCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner=run.id,
                lease_version=run.lease_version,
                expected_run_status=RuntimeRunStatus.RUNNING,
                target_run_status=RuntimeRunStatus.COMPLETED,
                expected_attempt_status=RuntimeAttemptStatus.RUNNING,
                target_attempt_status=RuntimeAttemptStatus.COMPLETED,
                events=(
                    RuntimeTerminalEvent(
                        event_type=RuntimeEventType.ATTEMPT_COMPLETED,
                        actor_type=RuntimeActorType.RUNTIME,
                        safe_payload={"status": "completed"},
                    ),
                    RuntimeTerminalEvent(
                        event_type=RuntimeEventType.RUN_COMPLETED,
                        actor_type=RuntimeActorType.RUNTIME,
                        safe_payload={"status": "completed"},
                    ),
                ),
            )
        )
        return runtime_store.get_run(run.id)

    left = long_run("run-long-left", 17, True)
    right = long_run("run-long-right", 3, False)

    result = RuntimeDiffService(store=runtime_store).compare(left.id, right.id)

    assert result.sections["metrics"].left["input_tokens"] == 17
    assert result.sections["metrics"].right["input_tokens"] == 3
    assert result.sections["evidence_references"].left == [evidence.id]
    assert result.sections["evidence_references"].right == []


def test_diff_uses_each_runs_frozen_business_projection(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")

    def save_hypothesis(cause_type: CauseType, confidence: float) -> None:
        repository.save(
            record.model_copy(
                update={
                    "hypotheses": [
                        Hypothesis(
                            cause_type=cause_type,
                            summary="free text must not enter Runtime Diff",
                            confidence=confidence,
                        )
                    ]
                }
            )
        )

    def completed_run(run_id: str) -> RuntimeRun:
        return runtime_store.create_run(
            RuntimeRun(
                id=run_id,
                investigation_id="inv-1",
                run_kind=RuntimeRunKind.LIVE,
                strategy=InvestigationStrategy.FIXED,
                status=RuntimeRunStatus.COMPLETED,
                run_reason=RuntimeRunReason.INITIAL,
                completed_at=datetime.now(UTC),
            )
        )

    save_hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.9)
    left = completed_run("run-frozen-left")
    save_hypothesis(CauseType.TRAFFIC_SPIKE, 0.6)
    right = completed_run("run-frozen-right")

    service = RuntimeDiffService(store=runtime_store)
    assert not hasattr(service, "business_repository")
    first = service.compare(left.id, right.id)
    save_hypothesis(CauseType.UNKNOWN, 0.1)
    second = service.compare(left.id, right.id)

    assert first.sections["causes"].left == [
        {"cause_type": "deployment_regression", "confidence": 0.9}
    ]
    assert first.sections["causes"].right == [{"cause_type": "traffic_spike", "confidence": 0.6}]
    assert first.sections["causes"] == second.sections["causes"]
    assert "evidence_references" not in runtime_store.get_frozen_business_projection(
        left.id
    )


def test_diff_never_rehydrates_a_missing_terminal_business_projection(
    runtime_store,
) -> None:
    run = runtime_store.create_run(
        RuntimeRun(
            id="run-missing-frozen-projection",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.INITIAL,
            completed_at=datetime.now(UTC),
        )
    )
    if hasattr(runtime_store, "_frozen_business_projections"):
        runtime_store._frozen_business_projections.pop(run.id)
    else:
        from backend.db.schema import runtime_runs

        with runtime_store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(frozen_business_projection=None)
            )

    result = RuntimeDiffService(store=runtime_store).compare(run.id, run.id)

    unavailable = {"status": "unavailable"}
    assert result.sections["causes"].left == unavailable
    assert result.sections["final_decision"].left == unavailable


def test_diff_ignores_unknown_types_and_future_schema_events() -> None:
    store, repository, left, _right = _fixture()
    service = RuntimeDiffService(store=store)
    baseline = service._project(left)
    base = datetime(2026, 7, 18, tzinfo=UTC)
    opaque_events = [
        RuntimeEvent(
            run_id=left.id,
            attempt_id=f"attempt-{left.id}",
            sequence=len(store._events[left.id]) + index,
            schema_version=schema_version,
            event_type=event_type,
            phase=RuntimePhase.SPECIALIST_ANALYSIS,
            actor_type=actor_type,
            actor_name="FutureAgent",
            evidence_ids=[f"opaque-evidence-{index}"],
            safe_payload=safe_payload,
            occurred_at=base + timedelta(seconds=index),
        )
        for index, (schema_version, event_type, actor_type, safe_payload) in enumerate(
            [
                (
                    1,
                    "phase.future",
                    RuntimeActorType.PHASE,
                    {"status": "completed", "failure_category": "unknown"},
                ),
                (
                    1,
                    "agent.future",
                    RuntimeActorType.AGENT,
                    {"status": "completed", "failure_category": "unknown"},
                ),
                (
                    1,
                    "arbitrary.future",
                    RuntimeActorType.RUNTIME,
                    {"status": "completed", "failure_category": "unknown"},
                ),
                (
                    2,
                    RuntimeEventType.MODEL_COMPLETED,
                    RuntimeActorType.MODEL,
                    {"status": "completed", "failure_category": "unknown"},
                ),
            ],
            start=1,
        )
    ]
    store._events[left.id].extend(opaque_events)

    assert service._project(left) == baseline
