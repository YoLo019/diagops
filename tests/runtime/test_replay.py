from datetime import UTC, datetime, timedelta

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseAttribution,
    RootCauseCandidate,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.reports import IncidentReport
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.runtime.diff import reseal_frozen_projection
from backend.runtime.faults import DeterministicFaultInjector
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.store import InMemoryRuntimeStore, RuntimeTerminalCommit, RuntimeTerminalEvent
from backend.runtime.writer import RuntimeEventCommand


def _replace_frozen_projection(store, run_id: str, value) -> None:
    if hasattr(store, "_frozen_business_projections"):
        if value is None:
            store._frozen_business_projections.pop(run_id, None)
        else:
            store._frozen_business_projections[run_id] = value
        return
    from backend.db.schema import runtime_runs

    with store.engine.begin() as connection:
        connection.execute(
            runtime_runs.update()
            .where(runtime_runs.c.id == run_id)
            .values(frozen_business_projection=value)
        )


def _complete_replay_fixture_run(
    store,
    *,
    run_id: str,
    evidence_id: str,
    run_reason: RuntimeRunReason,
    parent_run_id: str | None = None,
):
    run = store.create_run(
        RuntimeRun(
            id=run_id,
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=run_reason,
            parent_run_id=parent_run_id,
        )
    )
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            id=f"attempt-{run_id}",
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner=f"worker-{run_id}",
        expected_status=RuntimeRunStatus.CREATED,
    )
    for event_type in (
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.ATTEMPT_STARTED,
        RuntimeEventType.PHASE_STARTED,
        RuntimeEventType.PHASE_COMPLETED,
    ):
        is_phase = event_type in {
            RuntimeEventType.PHASE_STARTED,
            RuntimeEventType.PHASE_COMPLETED,
        }
        store.append_event_command(
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner=f"worker-{run_id}",
                lease_version=leased.lease_version,
                event_type=event_type,
                actor_type=(RuntimeActorType.PHASE if is_phase else RuntimeActorType.RUNTIME),
                phase=RuntimePhase.INTAKE if is_phase else None,
                evidence_ids=(
                    (evidence_id,)
                    if event_type == RuntimeEventType.PHASE_COMPLETED
                    else ()
                ),
                safe_payload={
                    "status": (
                        "running"
                        if event_type
                        in {
                            RuntimeEventType.RUN_STARTED,
                            RuntimeEventType.ATTEMPT_STARTED,
                            RuntimeEventType.PHASE_STARTED,
                        }
                        else "completed"
                    )
                },
            )
        )
    return store.commit_terminal(
        RuntimeTerminalCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner=f"worker-{run_id}",
            lease_version=leased.lease_version,
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


def _services():
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-replay",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="replay",
                description="replay fixture",
                started_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )
    )
    store = InMemoryRuntimeStore(repository)
    run = store.create_run(
        RuntimeRun(
            id="run-source",
            investigation_id="inv-replay",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.INITIAL,
            completed_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
    )
    store._events[run.id] = _valid_events(run.id)
    store._attempts["attempt-source"] = RuntimeAttempt(
        id="attempt-source",
        run_id=run.id,
        attempt_number=1,
        status=RuntimeAttemptStatus.COMPLETED,
        completed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    return store, repository, run


def _valid_events(run_id: str) -> list[RuntimeEvent]:
    types = [
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.ATTEMPT_STARTED,
        RuntimeEventType.PHASE_STARTED,
        RuntimeEventType.PHASE_COMPLETED,
        RuntimeEventType.ATTEMPT_COMPLETED,
        RuntimeEventType.RUN_COMPLETED,
    ]
    return [
        RuntimeEvent(
            id=f"event-{index}",
            run_id=run_id,
            attempt_id="attempt-source",
            sequence=index,
            event_type=event_type,
            phase=(
                RuntimePhase.INTAKE
                if event_type in {RuntimeEventType.PHASE_STARTED, RuntimeEventType.PHASE_COMPLETED}
                else None
            ),
            actor_type=(
                RuntimeActorType.PHASE
                if event_type in {RuntimeEventType.PHASE_STARTED, RuntimeEventType.PHASE_COMPLETED}
                else RuntimeActorType.RUNTIME
            ),
            safe_payload={
                "status": (
                    "running"
                    if event_type
                    in {
                        RuntimeEventType.RUN_STARTED,
                        RuntimeEventType.ATTEMPT_STARTED,
                        RuntimeEventType.PHASE_STARTED,
                    }
                    else "completed"
                )
            },
            occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        for index, event_type in enumerate(types, start=1)
    ]


def test_valid_replay_creates_terminal_replay_run_without_external_dependencies() -> None:
    store, repository, source = _services()
    dependencies = ReplayDependencies(store=store)
    assert not hasattr(dependencies, "business_repository")
    assert not hasattr(dependencies, "provider")
    assert not hasattr(dependencies, "tool_registry")
    assert not hasattr(dependencies, "model")

    report = ReplayService(dependencies).replay(source.id)
    replay_run = store.get_run(report.replay_run_id)

    assert report.valid is True
    assert report.external_call_count == 0
    assert report.benchmark_evaluation == "not_applicable"
    assert replay_run.run_kind == RuntimeRunKind.REPLAY
    assert replay_run.source_run_id == source.id
    assert replay_run.status == RuntimeRunStatus.COMPLETED


def test_historical_replay_isolated_from_completed_manual_rerun(runtime_store) -> None:
    repository = runtime_store.investigation_repository
    original_record = repository.get("inv-1")
    original_evidence = EvidenceItem(
        id="ev-original",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="original run evidence",
    )
    repository.save(original_record.model_copy(update={"evidence": [original_evidence]}))
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-original",
        evidence_id=original_evidence.id,
        run_reason=RuntimeRunReason.INITIAL,
    )
    service = ReplayService(
        ReplayDependencies(store=runtime_store)
    )
    assert service.replay(source.id).valid is True

    rerun_evidence = original_evidence.model_copy(
        update={"id": "ev-manual-rerun", "summary": "manual rerun evidence"}
    )
    current_record = repository.get("inv-1")
    repository.save(current_record.model_copy(update={"evidence": [rerun_evidence]}))
    _complete_replay_fixture_run(
        runtime_store,
        run_id="run-manual-rerun",
        evidence_id=rerun_evidence.id,
        run_reason=RuntimeRunReason.MANUAL_RERUN,
        parent_run_id=source.id,
    )

    report = service.replay(source.id)

    assert report.valid is True, report.validation_errors
    assert "bad_reference" not in report.validation_errors


def test_historical_legacy_payload_replay_preserves_attribution_order(
    runtime_store,
) -> None:
    # V10.1 兼容合同（spec R8、F17）：历史 run 的 Evidence payload 无 additive
    # 字段；frozen projection 不派生、不写回质量字段，且 review root_causes 保持
    # attribution 构建层顺序（全 legacy cohort 按 cluster score），不按 onset 重排。
    from backend.diagnosis.coordination_review import build_coordination_review

    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    base = datetime(2026, 7, 18, tzinfo=UTC)

    def legacy_metric(
        evidence_id: str, node: str, offset_seconds: float, deviation: float
    ) -> EvidenceItem:
        return EvidenceItem(
            id=evidence_id,
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=base + timedelta(seconds=offset_seconds),
            summary=f"{evidence_id} summary",
            payload={
                "component": "checkout",
                "node": node,
                "signal_type": "memory",
                "signal_name": "memory_usage",
                "deviation_score": deviation,
                "anomaly_segment_id": f"seg-{evidence_id}",
            },
        )

    weak = legacy_metric("ev-weak", "node-weak", 0, 1.0)
    strong = legacy_metric("ev-strong", "node-strong", 100, 5.0)
    evidence = [weak, strong]
    hypothesis = Hypothesis(
        cause_type=CauseType.RESOURCE_SATURATION,
        summary="Memory saturation",
        confidence=0.8,
        supporting_evidence_ids=[item.id for item in evidence],
    )
    review = build_coordination_review("inv-1", [], evidence, [hypothesis])
    repository.save(record.model_copy(update={"evidence": evidence}))
    repository.save_multi_agent_result(record.id, [], [], review)
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-legacy-no-drift",
        evidence_id=strong.id,
        run_reason=RuntimeRunReason.INITIAL,
    )

    frozen = runtime_store.get_frozen_business_projection(source.id)

    # cluster score 高（deviation 5）的 cluster 在前，尽管它的 onset 更晚；
    # 冻结与 reload 不得引入 onset 重排漂移。
    assert [
        item["supporting_evidence_ids"] for item in frozen["review"]["root_causes"]
    ] == [["ev-strong"], ["ev-weak"]]
    assert "strength_basis" not in str(frozen)
    assert "anomaly_point_count" not in str(frozen)
    replay = ReplayService(ReplayDependencies(store=runtime_store)).replay(source.id)
    assert replay.valid is True, replay.validation_errors


def test_frozen_projection_contains_typed_validator_inputs_without_cached_result(
    runtime_store,
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    evidence = EvidenceItem(
        id="ev-typed",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="sensitive-free-text evidence summary",
        payload={
            "service": "checkout-service",
            "component": "checkout-service",
            "signal_type": "deployment",
        },
    )
    hypothesis = Hypothesis(
        id="hyp-typed",
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="sensitive-free-text hypothesis prose",
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    action = RecommendedAction(
        id="act-typed",
        action_type=ActionType.CHECK,
        title="sensitive-free-text action title",
        description="sensitive-free-text action body",
        risk_level=ActionRiskLevel.READ_ONLY,
        requires_approval=False,
        supporting_evidence_ids=[evidence.id],
    )
    verification = VerificationSuggestion(
        id="ver-typed",
        title="sensitive-free-text verification title",
        description="sensitive-free-text verification body",
        expected_signal="sensitive-free-text expected signal",
        related_action_ids=[action.id],
    )
    report = IncidentReport(
        id="report-typed",
        investigation_id=record.id,
        summary="sensitive-free-text report summary",
        markdown="sensitive-free-text report markdown",
        hypotheses=[hypothesis],
        action_ids=[action.id],
        verification_suggestion_ids=[verification.id],
    )
    finding = AgentFinding(
        id="finding-typed",
        investigation_id=record.id,
        agent_name=AgentName.DEPLOYMENT,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="sensitive-free-text finding summary",
        confidence=0.8,
        evidence_ids=[evidence.id],
        related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
    )
    review = CoordinationReview(
        id="review-typed",
        investigation_id=record.id,
        candidates=[
            RootCauseCandidate(
                id="candidate-typed",
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                    summary="sensitive-free-text candidate summary",
                rank=1,
                confidence=0.8,
                supporting_finding_ids=[finding.id],
                supporting_evidence_ids=[evidence.id],
            )
        ],
        root_causes=[
            RootCauseAttribution(
                root_cause_occurred_at=evidence.timestamp,
                root_cause_component="checkout-service",
                root_cause_reason="deployment regression",
                supporting_evidence_ids=[evidence.id],
            )
        ],
    )
    repository.save(
        record.model_copy(
            update={
                "evidence": [evidence],
                "hypotheses": [hypothesis],
                "actions": [action],
                "verification_suggestions": [verification],
                "report": report,
            }
        )
    )
    repository.save_multi_agent_result(record.id, [finding], [], review)
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-typed-frozen",
        evidence_id=evidence.id,
        run_reason=RuntimeRunReason.INITIAL,
    )

    frozen = runtime_store.get_frozen_business_projection(source.id)

    assert frozen["schema_version"] == 1
    assert "replay_validation" not in frozen
    assert "business_validation_errors" not in frozen
    assert frozen["evidence"][0]["id"] == evidence.id
    assert frozen["findings"][0]["id"] == finding.id
    assert frozen["review"]["id"] == review.id
    assert frozen["review"]["root_causes"][0]["supporting_evidence_ids"] == [
        evidence.id
    ]
    assert frozen["report"]["id"] == report.id
    assert "sensitive-free-text" not in str(frozen)
    replay = ReplayService(ReplayDependencies(store=runtime_store)).replay(source.id)
    assert replay.valid is True, replay.validation_errors


@pytest.mark.parametrize(
    "environment",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "secret=ordinary-secret",
        "prod-secret",
        "prod-token",
        "prod-key",
        "https://operator:ordinary-secret@example.invalid/prod",
    ],
)
def test_frozen_projection_rejects_credential_like_environment(
    runtime_store, environment: str
) -> None:
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-safe-frozen-environment",
        evidence_id="evidence-1",
        run_reason=RuntimeRunReason.INITIAL,
    )
    frozen = runtime_store.get_frozen_business_projection(source.id)
    frozen["environment"] = environment

    with pytest.raises(ValueError, match="unsafe dynamic data"):
        reseal_frozen_projection(frozen)


def test_replay_reruns_semantic_validators_on_resealed_frozen_projection(
    runtime_store,
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    evidence = EvidenceItem(
        id="ev-semantic",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="semantic fixture",
    )
    finding = AgentFinding(
        id="finding-semantic",
        investigation_id=record.id,
        agent_name=AgentName.DEPLOYMENT,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="semantic fixture",
        confidence=0.8,
        evidence_ids=[evidence.id],
        related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
    )
    repository.save(record.model_copy(update={"evidence": [evidence]}))
    repository.save_multi_agent_result(record.id, [finding], [], None)
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-semantic-frozen",
        evidence_id=evidence.id,
        run_reason=RuntimeRunReason.INITIAL,
    )
    frozen = runtime_store.get_frozen_business_projection(source.id)
    if "findings" not in frozen:
        pytest.fail("frozen projection does not contain typed Finding inputs")
    frozen["findings"][0]["evidence_ids"] = ["ev-missing"]
    from backend.runtime.diff import reseal_frozen_projection

    _replace_frozen_projection(
        runtime_store,
        source.id,
        reseal_frozen_projection(frozen),
    )

    report = ReplayService(
        ReplayDependencies(store=runtime_store)
    ).replay(source.id)

    assert report.valid is False
    assert "business_finding_invalid" in report.validation_errors


@pytest.mark.parametrize(
    "mutation",
    [
        lambda frozen: frozen.update(evidence=None),
        lambda frozen: frozen.update(schema_version=2),
        lambda frozen: frozen["evidence"][0].update(provider="future-provider"),
        lambda frozen: frozen.update(integrity_sha256="0" * 64),
    ],
    ids=("shape", "schema", "enum", "digest"),
)
def test_corrupted_frozen_projection_returns_structured_invalid_report(
    runtime_store, mutation
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    evidence = EvidenceItem(
        id="ev-corrupt-frozen",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="corruption fixture",
    )
    repository.save(record.model_copy(update={"evidence": [evidence]}))
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-corrupt-frozen",
        evidence_id=evidence.id,
        run_reason=RuntimeRunReason.INITIAL,
    )
    frozen = runtime_store.get_frozen_business_projection(source.id)
    mutation(frozen)
    _replace_frozen_projection(runtime_store, source.id, frozen)

    report = ReplayService(
        ReplayDependencies(store=runtime_store)
    ).replay(source.id)

    assert report.valid is False
    assert report.external_call_count == 0
    assert "frozen_business_projection_invalid" in report.validation_errors


def test_missing_frozen_projection_is_unavailable_not_current_business_fallback(
    runtime_store,
) -> None:
    repository = runtime_store.investigation_repository
    record = repository.get("inv-1")
    evidence = EvidenceItem(
        id="ev-missing-frozen",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 18, tzinfo=UTC),
        summary="missing snapshot fixture",
    )
    repository.save(record.model_copy(update={"evidence": [evidence]}))
    source = _complete_replay_fixture_run(
        runtime_store,
        run_id="run-missing-frozen",
        evidence_id=evidence.id,
        run_reason=RuntimeRunReason.INITIAL,
    )
    _replace_frozen_projection(runtime_store, source.id, None)

    report = ReplayService(
        ReplayDependencies(store=runtime_store)
    ).replay(source.id)

    assert report.valid is False
    assert report.external_call_count == 0
    assert "frozen_business_projection_unavailable" in report.validation_errors


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (lambda events: setattr(events[2], "sequence", 4), "event_sequence_gap"),
        (lambda events: setattr(events[2], "sequence", 2), "event_sequence_duplicate"),
        (
            lambda events: setattr(events[3], "event_type", RuntimeEventType.PHASE_STARTED),
            "illegal_phase_transition",
        ),
        (lambda events: setattr(events[1], "schema_version", 2), "unsupported_schema"),
        (lambda events: events[2].evidence_ids.append("missing-evidence"), "bad_reference"),
    ],
)
def test_replay_detects_event_corruption(mutation, error_code: str) -> None:
    store, repository, source = _services()
    mutation(store._events[source.id])

    report = ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )

    assert report.valid is False
    assert error_code in report.validation_errors


def test_replay_detects_checkpoint_digest_corruption() -> None:
    store, repository, source = _services()
    checkpoint = RuntimeCheckpoint(
        id="checkpoint-bad",
        run_id=source.id,
        attempt_id="attempt-source",
        completed_phase=RuntimePhase.INTAKE,
        event_sequence=4,
        state_digest="f" * 64,
        projection_digest="0" * 64,
        resume_state=RuntimeResumeState(),
    )
    store._checkpoints[source.id] = checkpoint

    report = ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )

    assert "checkpoint_digest_mismatch" in report.validation_errors


@pytest.mark.parametrize(
    ("update", "error_code"),
    [
        ({"status": RuntimeAttemptStatus.FAILED}, "attempt_row_mismatch"),
        ({"attempt_number": 2}, "attempt_row_mismatch"),
        (
            {"resume_from_checkpoint_id": "missing-checkpoint"},
            "attempt_checkpoint_mismatch",
        ),
    ],
)
def test_replay_reconciles_persisted_attempt_rows(update: dict, error_code: str) -> None:
    store, repository, source = _services()
    store._attempts["attempt-source"] = store._attempts["attempt-source"].model_copy(update=update)

    report = ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )

    assert report.valid is False
    assert error_code in report.validation_errors


def test_replay_rejects_agent_lifecycle_outside_phase_scope() -> None:
    store, repository, source = _services()
    events = _valid_events(source.id)
    events[2:2] = [
        RuntimeEvent(
            run_id=source.id,
            attempt_id="attempt-source",
            sequence=3,
            event_type=event_type,
            actor_type=RuntimeActorType.AGENT,
            actor_name="LogAgent",
            safe_payload={"status": status},
        )
        for event_type, status in (
            (RuntimeEventType.AGENT_STARTED, "running"),
            (RuntimeEventType.AGENT_COMPLETED, "completed"),
        )
    ]
    for sequence, event in enumerate(events, start=1):
        event.sequence = sequence
    store._events[source.id] = events

    report = ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )

    assert report.valid is False
    assert "illegal_agent_transition" in report.validation_errors


def _replay_with_nested_events(definitions) -> object:
    store, repository, source = _services()
    events = _valid_events(source.id)
    nested = [
        RuntimeEvent(
            run_id=source.id,
            attempt_id="attempt-source",
            sequence=1,
            event_type=event_type,
            phase=RuntimePhase.INTAKE,
            actor_type=actor_type,
            actor_name=actor_name,
            execution_id=execution_id,
            safe_payload=safe_payload,
        )
        for event_type, actor_type, actor_name, execution_id, safe_payload in definitions
    ]
    events[3:3] = nested
    for sequence, event in enumerate(events, start=1):
        event.sequence = sequence
    store._events[source.id] = events
    return ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )


def test_replay_rejects_agent_terminal_before_its_owned_model() -> None:
    report = _replay_with_nested_events(
        [
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.MODEL_STARTED,
                RuntimeActorType.MODEL,
                "A",
                "model-a",
                {"status": "running"},
            ),
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "completed"},
            ),
            (
                RuntimeEventType.MODEL_COMPLETED,
                RuntimeActorType.MODEL,
                "A",
                "model-a",
                {"status": "completed"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "completed"},
            ),
        ]
    )

    assert report.valid is False
    assert "illegal_agent_transition" in report.validation_errors


def test_replay_rejects_model_and_tool_terminal_from_a_different_parent() -> None:
    report = _replay_with_nested_events(
        [
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.MODEL_STARTED,
                RuntimeActorType.MODEL,
                "A",
                "model-a",
                {"status": "running"},
            ),
            (
                RuntimeEventType.MODEL_COMPLETED,
                RuntimeActorType.MODEL,
                "B",
                "model-a",
                {"status": "completed"},
            ),
            (
                RuntimeEventType.TOOL_STARTED,
                RuntimeActorType.TOOL,
                "A",
                "tool-a",
                {"status": "running", "tool_name": "read_logs", "idempotency_key": "tool-a"},
            ),
            (
                RuntimeEventType.TOOL_COMPLETED,
                RuntimeActorType.TOOL,
                "B",
                "tool-a",
                {"status": "completed", "tool_name": "read_logs", "idempotency_key": "tool-a"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "completed"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "completed"},
            ),
        ]
    )

    assert report.valid is False
    assert "illegal_model_transition" in report.validation_errors
    assert "illegal_tool_transition" in report.validation_errors


def test_replay_allows_interleaved_specialists_with_owned_model_and_tool_work() -> None:
    report = _replay_with_nested_events(
        [
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.AGENT_STARTED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "running"},
            ),
            (
                RuntimeEventType.MODEL_STARTED,
                RuntimeActorType.MODEL,
                "A",
                "model-a",
                {"status": "running"},
            ),
            (
                RuntimeEventType.MODEL_STARTED,
                RuntimeActorType.MODEL,
                "B",
                "model-b",
                {"status": "running"},
            ),
            (
                RuntimeEventType.TOOL_STARTED,
                RuntimeActorType.TOOL,
                "A",
                "tool-a",
                {"status": "running", "tool_name": "read_logs", "idempotency_key": "tool-a"},
            ),
            (
                RuntimeEventType.MODEL_COMPLETED,
                RuntimeActorType.MODEL,
                "B",
                "model-b",
                {"status": "completed"},
            ),
            (
                RuntimeEventType.TOOL_COMPLETED,
                RuntimeActorType.TOOL,
                "A",
                "tool-a",
                {"status": "completed", "tool_name": "read_logs", "idempotency_key": "tool-a"},
            ),
            (
                RuntimeEventType.MODEL_COMPLETED,
                RuntimeActorType.MODEL,
                "A",
                "model-a",
                {"status": "completed"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "A",
                None,
                {"status": "completed"},
            ),
            (
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeActorType.AGENT,
                "B",
                None,
                {"status": "completed"},
            ),
        ]
    )

    assert report.valid is True, report.validation_errors


def test_replay_external_call_fault_is_a_structured_safety_rejection() -> None:
    store, repository, source = _services()
    service = ReplayService(
        ReplayDependencies(store=store),
        fault_injector=DeterministicFaultInjector({"replay_external_call": 1}),
    )

    report = service.replay(source.id)

    assert report.valid is False
    assert report.external_call_count == 1
    assert report.validation_errors == ["external_call_attempt"]


def test_replay_reads_all_event_pages_for_long_memory_and_sqlite_runs(
    runtime_store,
) -> None:
    run = runtime_store.create_run(
        RuntimeRun(
            id="run-long-replay",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    attempt = RuntimeAttempt(
        id="attempt-long-replay",
        run_id=run.id,
        attempt_number=1,
        status=RuntimeAttemptStatus.RUNNING,
    )
    run, attempt = runtime_store.acquire_lease_and_create_attempt(
        run.id,
        attempt=attempt,
        owner="replay-test",
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
                lease_owner="replay-test",
                lease_version=run.lease_version,
                event_type=event_type,
                actor_type=RuntimeActorType.RUNTIME,
                safe_payload={"status": "running"},
            )
        )
    runtime_store.append_event_command(
        RuntimeEventCommand(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="replay-test",
            lease_version=run.lease_version,
            event_type=RuntimeEventType.PHASE_STARTED,
            actor_type=RuntimeActorType.PHASE,
            phase=RuntimePhase.EVIDENCE_COLLECTION,
            safe_payload={"status": "running"},
        )
    )
    for _index in range(500):
        runtime_store.append_event_command(
            RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="replay-test",
                lease_version=run.lease_version,
                event_type=RuntimeEventType.EVIDENCE_REJECTED,
                actor_type=RuntimeActorType.PROVIDER,
                phase=RuntimePhase.EVIDENCE_COLLECTION,
                safe_payload={"status": "rejected"},
            )
        )
    runtime_store.append_event_command(
        RuntimeEventCommand(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="replay-test",
            lease_version=run.lease_version,
            event_type=RuntimeEventType.PHASE_COMPLETED,
            actor_type=RuntimeActorType.PHASE,
            phase=RuntimePhase.EVIDENCE_COLLECTION,
            safe_payload={"status": "completed"},
        )
    )
    runtime_store.commit_terminal(
        RuntimeTerminalCommit(
            run_id=run.id,
            attempt_id=attempt.id,
            lease_owner="replay-test",
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

    report = ReplayService(
        ReplayDependencies(
            store=runtime_store,
        )
    ).replay(run.id)

    assert report.valid is True


@pytest.mark.parametrize(
    ("event_type", "error_code"),
    [
        (RuntimeEventType.RUN_COMPLETED, "illegal_run_transition"),
        (RuntimeEventType.ATTEMPT_COMPLETED, "illegal_attempt_transition"),
        (RuntimeEventType.PHASE_COMPLETED, "illegal_phase_transition"),
        (RuntimeEventType.AGENT_COMPLETED, "illegal_agent_transition"),
        (RuntimeEventType.MODEL_COMPLETED, "illegal_model_transition"),
        (RuntimeEventType.TOOL_COMPLETED, "illegal_tool_transition"),
        (RuntimeEventType.RECOVERY_COMPLETED, "illegal_recovery_transition"),
        (RuntimeEventType.EVIDENCE_PERSISTED, "illegal_evidence_transition"),
    ],
)
def test_replay_rejects_orphan_lifecycle_events(
    event_type: RuntimeEventType,
    error_code: str,
) -> None:
    store, repository, source = _services()
    orphan = RuntimeEvent(
        id="event-orphan",
        run_id=source.id,
        attempt_id="attempt-source",
        sequence=3,
        event_type=event_type,
        phase=(RuntimePhase.INTAKE if event_type == RuntimeEventType.PHASE_COMPLETED else None),
        actor_type=(
            RuntimeActorType.PHASE
            if event_type == RuntimeEventType.PHASE_COMPLETED
            else RuntimeActorType.RUNTIME
        ),
        actor_name=("LogAgent" if event_type == RuntimeEventType.AGENT_COMPLETED else None),
        safe_payload={
            "status": "completed",
            **({"evidence_count": 1} if event_type == RuntimeEventType.EVIDENCE_PERSISTED else {}),
        },
        occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    events = _valid_events(source.id)
    events.insert(2, orphan)
    for sequence, event in enumerate(events, start=1):
        event.sequence = sequence
    store._events[source.id] = events

    report = ReplayService(ReplayDependencies(store=store)).replay(
        source.id
    )

    assert report.valid is False
    assert error_code in report.validation_errors


@pytest.mark.parametrize(
    "status",
    [RuntimeRunStatus.COMPLETED, RuntimeRunStatus.FAILED],
)
def test_replay_rejects_terminal_run_without_execution_history(status) -> None:
    store, repository, _source = _services()
    empty = store.create_run(
        RuntimeRun(
            id=f"run-empty-{status.value}",
            investigation_id="inv-replay",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=status,
            run_reason=RuntimeRunReason.INITIAL,
            completed_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
    )

    report = ReplayService(
        ReplayDependencies(store=store)
    ).replay(empty.id)

    assert report.valid is False
    assert "execution_history_missing" in report.validation_errors


def test_replay_explicitly_accepts_created_then_cancelled_without_execution() -> None:
    store, repository, _source = _services()
    cancelled = store.create_run(
        RuntimeRun(
            id="run-cancelled-before-start",
            investigation_id="inv-replay",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    cancelled = store.request_cancel(cancelled.id)

    report = ReplayService(
        ReplayDependencies(store=store)
    ).replay(cancelled.id)

    assert cancelled.cancel_requested_at is not None
    assert report.valid is True
