from inspect import signature
from math import inf, nan

import pytest
from pydantic import ValidationError

from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    ALLOWED_TRANSITIONS,
    V11_DEFAULT_MAX_INVESTIGATORS,
    V11_DEFAULT_MAX_ROUNDS,
    V11_DEFAULT_MAX_TURNS,
    V11_DEFAULT_TOOL_BUDGET,
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
    V11ExecutionContractInput,
    build_v11_execution_contract,
    ensure_run_transition,
    validate_v11_execution_contract,
)


def test_runtime_event_rejects_non_allowlisted_payload_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.RUN_CREATED,
            actor_type=RuntimeActorType.RUNTIME,
            safe_payload={"prompt": "secret"},
        )


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_runtime_event_rejects_non_finite_json(value: float) -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.PHASE_COMPLETED,
            actor_type=RuntimeActorType.PHASE,
            safe_payload={"duration_ms": value},
        )


def test_runtime_event_rejects_nested_prohibited_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.TOOL_COMPLETED,
            actor_type=RuntimeActorType.TOOL,
            safe_payload={"status": "completed", "metadata": {"authorization": "secret"}},
        )


def test_runtime_event_redacts_and_bounds_safe_message() -> None:
    event = RuntimeEvent(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.RUN_FAILED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"message": "Bearer super-secret " + "x" * 1000},
    )

    assert "super-secret" not in event.safe_payload["message"]
    assert len(event.safe_payload["message"]) <= 512


def test_runtime_event_redacts_bare_provider_credential_in_message() -> None:
    event = RuntimeEvent(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.RUN_FAILED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"message": "provider failed sk-proj-abcdefghijklmnopqrstuvwxyz123456"},
    )

    assert "sk-proj" not in event.safe_payload["message"]


def test_runtime_model_event_accepts_bounded_input_estimate_audit() -> None:
    event = RuntimeEvent(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.MODEL_STARTED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={
            "status": "started",
            "input_estimate_audit": {
                "method": "unicode-json-envelope-v1",
                "estimated_tokens": 120,
                "instruction_chars": 240,
                "context_chars": 180,
                "total_chars": 420,
            },
        },
    )

    assert event.safe_payload["input_estimate_audit"]["estimated_tokens"] == 120


@pytest.mark.parametrize(
    "payload",
    [
        {"metadata": {"apiKey": "secret"}},
        {"metadata": {"rawPayload": {"body": "unsafe"}}},
        {"normalized_inputs": {"query": "Bearer hidden-token"}},
        {"metadata": {"a": {"b": {"c": {"d": {"e": 1}}}}}},
        {"metadata": {str(index): index for index in range(40)}},
    ],
)
def test_runtime_event_rejects_unsafe_or_unbounded_structured_payload(payload) -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.TOOL_COMPLETED,
            actor_type=RuntimeActorType.TOOL,
            safe_payload=payload,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "read_logs", "normalized_inputs": {"query": "error OR panic"}},
        {"tool_name": "read_logs", "metadata": {"data": {"body": "raw log body"}}},
        {"tool_name": "read_logs", "metadata": {"provider_response": "raw prose"}},
        {"tool_name": "read_logs", "metadata": {"details": {"payload": {"body": "raw"}}}},
        {
            "tool_name": "read_logs",
            "normalized_inputs": {
                "reason": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            },
        },
    ],
)
def test_runtime_event_rejects_non_allowlisted_tool_payload(payload) -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.TOOL_COMPLETED,
            actor_type=RuntimeActorType.TOOL,
            safe_payload=payload,
        )


def test_runtime_event_accepts_allowlisted_structured_tool_payload() -> None:
    event = RuntimeEvent(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.TOOL_COMPLETED,
        actor_type=RuntimeActorType.TOOL,
        safe_payload={
            "tool_name": "read_logs",
            "normalized_inputs": {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "limit": 20,
                "keywords": ["timeout", "retry"],
                "levels": ["ERROR"],
                "instance": "checkout-01",
            },
            "metadata": {"result_count": 2, "provider_status": "success"},
        },
    )

    assert event.safe_payload["metadata"]["result_count"] == 2


@pytest.mark.parametrize(
    ("tool_name", "normalized_inputs"),
    [
        (
            "query_related_alerts",
            {
                "window_start": "2026-07-15T07:50:00+00:00",
                "window_end": "2026-07-15T08:10:00+00:00",
                "limit": 50,
                "severities": ["critical"],
                "statuses": ["firing", "resolved"],
                "entity_ids": ["checkout-service"],
            },
        ),
        (
            "query_traces",
            {
                "window_start": "2026-07-15T07:50:00+00:00",
                "window_end": "2026-07-15T08:10:00+00:00",
                "limit": 50,
                "service": "checkout-service",
                "trace_id": "a" * 32,
                "error_only": False,
                "min_duration_ms": 12.5,
                "direction": "both",
            },
        ),
        (
            "read_runtime_state",
            {
                "limit": 50,
                "states": ["crash_loop", "oom_killed"],
                "include_healthy": False,
            },
        ),
        (
            "lookup_memory",
            {
                "affected_entity": "checkout-service",
                "failure_mechanism": "database connection pool exhausted",
                "limit": 5,
            },
        ),
        (
            "read_logs",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "limit": 20,
                "keywords": ["connection timeout", "pool exhausted"],
            },
        ),
        (
            "read_logs",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "levels": ["free text level"],
                "instance": "checkout pod 01",
            },
        ),
        (
            "query_metrics",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "metric_names": ["custom.metric rate"],
                "instance": "",
            },
        ),
        (
            "read_deployments",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "version": "v1.2.3 canary",
            },
        ),
        (
            "query_dependencies",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "target": "checkout service",
            },
        ),
        (
            "read_service_catalog",
            {
                "start_time": "2026-07-17T00:00:00Z",
                "end_time": "2026-07-17T00:05:00Z",
                "name": "checkout service",
            },
        ),
    ],
)
def test_runtime_event_accepts_scoped_tool_payloads(
    tool_name, normalized_inputs
) -> None:
    event = RuntimeEvent(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.TOOL_COMPLETED,
        actor_type=RuntimeActorType.TOOL,
        safe_payload={
            "status": "success",
            "tool_name": tool_name,
            "idempotency_key": "run-1:tool:1",
            "normalized_inputs": normalized_inputs,
        },
    )

    reloaded = RuntimeEvent.model_validate(event.model_dump(mode="json"))
    assert reloaded.safe_payload["normalized_inputs"] == normalized_inputs


@pytest.mark.parametrize(
    ("tool_name", "normalized_inputs"),
    [
        ("query_related_alerts", {"query": "error OR panic"}),
        ("query_related_alerts", {"severities": ["critical; drop"]}),
        ("query_traces", {"min_duration_ms": -1}),
        ("query_traces", {"min_duration_ms": "fast"}),
        ("query_traces", {"direction": "sideways"}),
        ("lookup_memory", {"failure_mechanism": "x" * 257}),
        ("read_logs", {"keywords": ["x" * 257]}),
        ("read_logs", {"keywords": [""]}),
        ("read_logs", {"instance": "x" * 257}),
        ("query_metrics", {"metric_names": ["x" * 257]}),
        ("read_service_catalog", {"name": "x" * 257}),
        ("read_service_catalog", {"bogus": 1}),
        ("query_related_alerts", {"entity_ids": ["has space"]}),
        ("made_up_tool", {"limit": 1}),
    ],
)
def test_runtime_event_rejects_out_of_contract_scoped_tool_payloads(
    tool_name, normalized_inputs
) -> None:
    with pytest.raises(ValidationError):
        RuntimeEvent(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            event_type=RuntimeEventType.TOOL_COMPLETED,
            actor_type=RuntimeActorType.TOOL,
            safe_payload={
                "status": "success",
                "tool_name": tool_name,
                "normalized_inputs": normalized_inputs,
            },
        )


def test_checkpoint_contains_control_state_not_business_payload() -> None:
    checkpoint = RuntimeCheckpoint(
        run_id="run-1",
        attempt_id="attempt-1",
        completed_phase=RuntimePhase.EVIDENCE_COLLECTION,
        event_sequence=4,
        state_digest="a" * 64,
        resume_state=RuntimeResumeState(
            completed_evidence_ids=["evidence-1"],
            remaining_tool_budget=3,
            successful_tool_keys=["tool-key-1"],
        ),
    )

    assert "evidence" not in checkpoint.resume_state.model_dump()


def test_runtime_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeResumeState.model_validate({"evidence": {"raw": "body"}})


def test_runtime_run_and_attempt_contract_defaults() -> None:
    run = RuntimeRun(
        id="run-1",
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        status=RuntimeRunStatus.CREATED,
        run_reason=RuntimeRunReason.INITIAL,
    )
    attempt = RuntimeAttempt(
        id="attempt-1",
        run_id=run.id,
        attempt_number=1,
        status=RuntimeAttemptStatus.RUNNING,
    )

    assert run.lease_version == 0
    assert run.current_phase is None
    assert run.timeout_seconds == 60.0
    assert attempt.resume_from_checkpoint_id is None


def test_runtime_run_timeout_is_a_positive_frozen_value() -> None:
    run = RuntimeRun(
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.INITIAL,
        timeout_seconds=1.25,
    )

    assert run.timeout_seconds == 1.25
    with pytest.raises(ValidationError):
        RuntimeRun.model_validate({**run.model_dump(), "timeout_seconds": 0})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    ],
)
def test_every_approved_runtime_transition_is_accepted(
    source: RuntimeRunStatus, target: RuntimeRunStatus
) -> None:
    ensure_run_transition(source, target)


@pytest.mark.parametrize(
    "terminal",
    [RuntimeRunStatus.COMPLETED, RuntimeRunStatus.FAILED, RuntimeRunStatus.CANCELLED],
)
def test_terminal_runtime_status_rejects_all_transitions(
    terminal: RuntimeRunStatus,
) -> None:
    assert ALLOWED_TRANSITIONS[terminal] == frozenset()
    with pytest.raises(ValueError):
        ensure_run_transition(terminal, RuntimeRunStatus.RUNNING)


def test_unapproved_runtime_transition_is_rejected() -> None:
    with pytest.raises(ValueError):
        ensure_run_transition(RuntimeRunStatus.CREATED, RuntimeRunStatus.COMPLETED)


@pytest.mark.parametrize(
    ("run_kind", "run_reason", "source_run_id", "parent_run_id"),
    [
        (RuntimeRunKind.LIVE, RuntimeRunReason.REPLAY, None, None),
        (RuntimeRunKind.REPLAY, RuntimeRunReason.REPLAY, None, None),
        (RuntimeRunKind.REPLAY, RuntimeRunReason.REPLAY, "run-source", "run-parent"),
        (RuntimeRunKind.LIVE, RuntimeRunReason.INITIAL, None, "run-parent"),
        (RuntimeRunKind.LIVE, RuntimeRunReason.MANUAL_RERUN, None, None),
        (RuntimeRunKind.LIVE, RuntimeRunReason.ADDITIONAL_EVIDENCE, None, None),
    ],
)
def test_runtime_run_rejects_invalid_kind_reason_and_lineage_combinations(
    run_kind,
    run_reason,
    source_run_id,
    parent_run_id,
) -> None:
    with pytest.raises(ValidationError):
        RuntimeRun(
            investigation_id="inv-1",
            run_kind=run_kind,
            strategy=InvestigationStrategy.FIXED,
            run_reason=run_reason,
            source_run_id=source_run_id,
            parent_run_id=parent_run_id,
        )


def test_runtime_run_accepts_valid_live_and_replay_lineage() -> None:
    rerun = RuntimeRun(
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.MANUAL_RERUN,
        parent_run_id="run-source",
    )
    replay = RuntimeRun(
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.REPLAY,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.REPLAY,
        source_run_id="run-source",
    )

    assert rerun.parent_run_id == replay.source_run_id == "run-source"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_name", "sk-proj-abcdefghijklmnopqrstuvwxyz123456"),
        ("prompt_version", "secret=benchmark-token"),
        ("model_name", "https://user:password@example.test/model"),
        ("prompt_version", "x" * 161),
    ],
)
def test_runtime_run_rejects_sensitive_or_unbounded_public_metadata(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        RuntimeRun(
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
            **{field: value},
        )


@pytest.mark.parametrize(
    "model_name",
    ["gpt-4.1-mini", "deepseek-chat", "openai/gpt-oss-120b", "azure:gpt-4o"],
)
def test_runtime_run_accepts_realistic_model_identifiers(model_name: str) -> None:
    run = RuntimeRun(
        investigation_id="inv-1",
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.INITIAL,
        model_name=model_name,
        prompt_version="v9.1-production",
    )

    assert run.model_name == model_name


def test_v11_contract_builder_is_the_shared_budget_source() -> None:
    """产品默认值、runtime 默认值与契约拓扑不能各自漂移。"""
    from backend.benchmarks.rcaeval.models import (
        EXPECTED_CONFIGURATION_TOPOLOGY,
        RcaEvalConfiguration,
    )
    from backend.config.settings import AgentsSettings
    from backend.diagnosis.v11_runtime import V11Runtime

    settings = AgentsSettings()
    assert settings.max_turns == V11_DEFAULT_MAX_TURNS
    assert settings.max_total_tool_calls == V11_DEFAULT_TOOL_BUDGET
    runtime_signature = signature(V11Runtime)
    assert runtime_signature.parameters["max_turns"].default == V11_DEFAULT_MAX_TURNS
    assert (
        runtime_signature.parameters["max_total_tool_calls"].default
        == V11_DEFAULT_TOOL_BUDGET
    )
    assert EXPECTED_CONFIGURATION_TOPOLOGY[RcaEvalConfiguration.MULTI_INTENDED] == (
        V11_DEFAULT_MAX_INVESTIGATORS,
        V11_DEFAULT_MAX_ROUNDS,
    )

    contract = build_v11_execution_contract(
        V11ExecutionContractInput(
            model_provider="openai",
            model_name="drift-test",
            prompt_version="v11-test",
            api_mode="responses",
            tool_manifest=("read_logs",),
            skill_catalog={},
            token_budget=100,
        )
    )
    validate_v11_execution_contract(contract)
    assert contract["limits"]["max_turns"] == V11_DEFAULT_MAX_TURNS
    assert contract["tool_budget"] == V11_DEFAULT_TOOL_BUDGET
