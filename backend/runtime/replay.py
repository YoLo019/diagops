from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.diagnosis.evidence_validation import (
    validate_agent_semantics,
    validate_hypotheses,
)
from backend.domain.runtime import (
    ReplayReport,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.runtime.faults import NoFaultInjector, RuntimeInjectedFault
from backend.runtime.phases import checkpoint_digest

_EVENT_PAGE_SIZE = 500


def list_all_runtime_events(store, run_id: str) -> list[Any]:
    """按 sequence 游标读取完整事件流；页尾 sequence 是下一页唯一游标。"""
    events: list[Any] = []
    after = 0
    while True:
        page = store.list_events(run_id, after=after, limit=_EVENT_PAGE_SIZE)
        if not page:
            break
        events.extend(page)
        next_after = page[-1].sequence
        if next_after <= after:
            # Store 合同要求 sequence 单调；防止损坏数据令审计无限循环。
            break
        after = next_after
        if len(page) < _EVENT_PAGE_SIZE:
            break
    return events


@dataclass(slots=True)
class _LifecycleState:
    run_status: str = "created"
    active_attempt_id: str | None = None
    started_attempt_ids: set[str] = None  # type: ignore[assignment]
    open_phase: Any | None = None
    open_agents: set[str] = None  # type: ignore[assignment]
    open_models: dict[str, str] = None  # type: ignore[assignment]
    tool_states: dict[str, str] = None  # type: ignore[assignment]
    tool_parents: dict[str, str] = None  # type: ignore[assignment]
    recovery_attempt_id: str | None = None
    attempt_order: list[str] = None  # type: ignore[assignment]
    attempt_terminal_status: dict[str, str] = None  # type: ignore[assignment]
    recovery_checkpoint_ids: dict[str, str | None] = None  # type: ignore[assignment]
    recovery_attempt_numbers: dict[str, int | None] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.started_attempt_ids = set()
        self.open_agents = set()
        self.open_models = {}
        self.tool_states = {}
        self.tool_parents = {}
        self.attempt_order = []
        self.attempt_terminal_status = {}
        self.recovery_checkpoint_ids = {}
        self.recovery_attempt_numbers = {}


@dataclass(frozen=True, slots=True)
class ReplayDependencies:
    """Replay 只接收持久化读取能力，类型上不暴露 Provider、Tool 或 model。"""

    store: object
    benchmark_evaluator: Callable[[str], str] | None = None


class ReplayService:
    def __init__(self, dependencies: ReplayDependencies, *, fault_injector=None) -> None:
        self.dependencies = dependencies
        self.fault_injector = fault_injector or NoFaultInjector()

    def replay(self, source_run_id: str) -> ReplayReport:
        source = self.dependencies.store.get_run(source_run_id)
        try:
            self.fault_injector.hit("replay_external_call")
        except RuntimeInjectedFault:
            return self._report(
                source,
                errors=["external_call_attempt"],
                external_call_count=1,
            )

        errors: list[str] = []
        if source.status not in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        }:
            errors.append("source_run_not_terminal")
        events = list_all_runtime_events(self.dependencies.store, source.id)
        attempts = self.dependencies.store.list_attempts(source.id)
        checkpoints = self.dependencies.store.list_checkpoints(source.id)
        cancelled_before_start = (
            source.status == RuntimeRunStatus.CANCELLED
            and source.cancel_requested_at is not None
            and not attempts
            and not checkpoints
            and not events
        )
        if not events and not cancelled_before_start:
            errors.append("execution_history_missing")
        frozen = self.dependencies.store.get_frozen_business_projection(source.id)
        projection = None
        if frozen is None:
            errors.append("frozen_business_projection_unavailable")
        else:
            from backend.runtime.diff import parse_frozen_projection

            try:
                projection = parse_frozen_projection(frozen)
            except (TypeError, ValueError):
                errors.append("frozen_business_projection_invalid")
        errors.extend(self._validate_events(source, events, projection))
        errors.extend(self._validate_checkpoints(source, projection))
        errors.extend(self._validate_business_projection(source, projection))
        benchmark, benchmark_error = self._benchmark_evaluation(source, projection)
        if benchmark_error is not None:
            errors.append(benchmark_error)
        return self._report(source, errors=errors, benchmark=benchmark)

    def _validate_events(self, source, events, projection) -> list[str]:
        errors: list[str] = []
        seen: set[int] = set()
        expected = 1
        lifecycle = _LifecycleState()
        evidence_ids = {item.id for item in projection.evidence} if projection else set()
        task_ids = set(projection.task_ids) if projection else set()
        execution_ids = set(projection.execution_ids) if projection else set()
        tool_ids = set(projection.tool_call_ids) if projection else set()
        checkpoints = {
            item.id: item for item in self.dependencies.store.list_checkpoints(source.id)
        }
        attempts = {item.id: item for item in self.dependencies.store.list_attempts(source.id)}
        for event in events:
            if event.sequence in seen:
                errors.append("event_sequence_duplicate")
            elif event.sequence != expected:
                errors.append("event_sequence_gap")
            seen.add(event.sequence)
            expected = max(expected, event.sequence + 1)
            if event.schema_version != 1:
                errors.append("unsupported_schema")
            if not isinstance(event.event_type, RuntimeEventType):
                errors.append("unsupported_event_type")
            if event.run_id != source.id:
                errors.append("bad_reference")
            attempt = attempts.get(event.attempt_id)
            if attempt is None or attempt.run_id != source.id:
                errors.append("bad_reference")
            if any(item not in evidence_ids for item in event.evidence_ids):
                errors.append("bad_reference")
            if event.task_id is not None and event.task_id not in task_ids:
                errors.append("bad_reference")
            if (
                event.execution_id is not None
                and isinstance(event.event_type, RuntimeEventType)
                and event.event_type.value.startswith("agent.")
                and event.execution_id not in execution_ids
            ):
                errors.append("bad_reference")
            if event.tool_call_id is not None and event.tool_call_id not in tool_ids:
                errors.append("bad_reference")
            checkpoint_id = event.safe_payload.get("checkpoint_id")
            if checkpoint_id is not None and checkpoint_id not in checkpoints:
                errors.append("bad_reference")
            if event.schema_version == 1 and isinstance(event.event_type, RuntimeEventType):
                errors.extend(self._advance_lifecycle(lifecycle, event))
        errors.extend(self._validate_attempt_rows(lifecycle, attempts, checkpoints))
        if events and lifecycle.run_status != source.status.value:
            errors.append("illegal_run_transition")
        if lifecycle.open_phase is not None:
            errors.append("illegal_phase_transition")
        if lifecycle.open_agents:
            errors.append("illegal_agent_transition")
        if lifecycle.open_models:
            errors.append("illegal_model_transition")
        if any(status in {"proposed", "started"} for status in lifecycle.tool_states.values()):
            errors.append("illegal_tool_transition")
        if lifecycle.recovery_attempt_id is not None:
            errors.append("illegal_recovery_transition")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _validate_attempt_rows(lifecycle, attempts, checkpoints) -> list[str]:
        errors: list[str] = []
        if set(attempts) != set(lifecycle.attempt_order):
            errors.append("attempt_row_mismatch")
        for expected_number, attempt_id in enumerate(lifecycle.attempt_order, start=1):
            attempt = attempts.get(attempt_id)
            if attempt is None:
                continue
            if (
                attempt.attempt_number != expected_number
                or lifecycle.attempt_terminal_status.get(attempt_id) != attempt.status.value
            ):
                errors.append("attempt_row_mismatch")
            resume_checkpoint_id = attempt.resume_from_checkpoint_id
            if expected_number == 1:
                if (
                    resume_checkpoint_id is not None
                    or attempt_id in lifecycle.recovery_checkpoint_ids
                ):
                    errors.append("attempt_checkpoint_mismatch")
                continue
            if attempt_id not in lifecycle.recovery_checkpoint_ids:
                errors.append("attempt_checkpoint_mismatch")
                continue
            if (
                lifecycle.recovery_checkpoint_ids[attempt_id] != resume_checkpoint_id
                or lifecycle.recovery_attempt_numbers.get(attempt_id) != attempt.attempt_number
            ):
                errors.append("attempt_checkpoint_mismatch")
            if resume_checkpoint_id is not None:
                checkpoint = checkpoints.get(resume_checkpoint_id)
                if checkpoint is None or checkpoint.attempt_id == attempt_id:
                    errors.append("attempt_checkpoint_mismatch")
        return errors

    @staticmethod
    def _entity_key(event, *, include_actor: bool = False) -> str | None:
        key = event.execution_id or event.task_id
        if key is None and include_actor:
            key = event.actor_name
        return key

    def _advance_lifecycle(self, state: _LifecycleState, event) -> list[str]:
        errors: list[str] = []
        event_type = event.event_type
        if event_type == RuntimeEventType.RUN_CREATED:
            if state.run_status != "created" or state.started_attempt_ids:
                errors.append("illegal_run_transition")
        elif event_type == RuntimeEventType.RUN_STARTED:
            if state.run_status not in {"created", "interrupted"}:
                errors.append("illegal_run_transition")
            state.run_status = "running"
        elif event_type == RuntimeEventType.RUN_CANCEL_REQUESTED:
            if state.run_status != "running":
                errors.append("illegal_run_transition")
            state.run_status = "cancelling"
        elif event_type == RuntimeEventType.RUN_INTERRUPTED:
            if state.run_status not in {"running", "cancelling"}:
                errors.append("illegal_run_transition")
            state.run_status = "interrupted"
            self._close_interrupted_work(state, clear_attempt=False)
        elif event_type in {
            RuntimeEventType.RUN_COMPLETED,
            RuntimeEventType.RUN_FAILED,
            RuntimeEventType.RUN_CANCELLED,
        }:
            expected = {
                RuntimeEventType.RUN_COMPLETED: "running",
                RuntimeEventType.RUN_FAILED: "running",
                RuntimeEventType.RUN_CANCELLED: "cancelling",
            }[event_type]
            if state.run_status != expected:
                errors.append("illegal_run_transition")
            if event_type == RuntimeEventType.RUN_COMPLETED and state.active_attempt_id:
                errors.append("illegal_attempt_transition")
            if (
                event_type
                in {
                    RuntimeEventType.RUN_FAILED,
                    RuntimeEventType.RUN_CANCELLED,
                }
                and state.active_attempt_id is not None
            ):
                state.attempt_terminal_status[state.active_attempt_id] = {
                    RuntimeEventType.RUN_FAILED: "failed",
                    RuntimeEventType.RUN_CANCELLED: "cancelled",
                }[event_type]
            state.run_status = event_type.value.split(".", 1)[1]
            if event_type != RuntimeEventType.RUN_COMPLETED:
                self._close_interrupted_work(state)
        elif event_type == RuntimeEventType.ATTEMPT_STARTED:
            if (
                state.run_status != "running"
                or state.active_attempt_id is not None
                or event.attempt_id in state.started_attempt_ids
            ):
                errors.append("illegal_attempt_transition")
            state.active_attempt_id = event.attempt_id
            state.started_attempt_ids.add(event.attempt_id)
            state.attempt_order.append(event.attempt_id)
        elif event_type == RuntimeEventType.ATTEMPT_COMPLETED:
            if (
                state.run_status != "running"
                or state.active_attempt_id != event.attempt_id
                or self._has_open_work(state)
            ):
                errors.append("illegal_attempt_transition")
            state.active_attempt_id = None
            state.attempt_terminal_status[event.attempt_id] = "completed"
        elif event_type == RuntimeEventType.ATTEMPT_INTERRUPTED:
            if state.run_status != "interrupted" or state.active_attempt_id != event.attempt_id:
                errors.append("illegal_attempt_transition")
            state.attempt_terminal_status[event.attempt_id] = "interrupted"
            self._close_interrupted_work(state)
        elif event_type == RuntimeEventType.PHASE_STARTED:
            if (
                state.run_status != "running"
                or state.active_attempt_id != event.attempt_id
                or state.open_phase is not None
                or event.phase is None
            ):
                errors.append("illegal_phase_transition")
            state.open_phase = event.phase
        elif event_type in {
            RuntimeEventType.PHASE_COMPLETED,
            RuntimeEventType.PHASE_FAILED,
            RuntimeEventType.PHASE_SKIPPED,
        }:
            if (
                state.active_attempt_id != event.attempt_id
                or state.open_phase is None
                or event.phase != state.open_phase
                or self._has_open_phase_work(state)
            ):
                errors.append("illegal_phase_transition")
            state.open_phase = None
        elif event_type in {
            RuntimeEventType.AGENT_STARTED,
            RuntimeEventType.AGENT_COMPLETED,
            RuntimeEventType.AGENT_FAILED,
        }:
            key = event.actor_name
            if event_type == RuntimeEventType.AGENT_STARTED:
                if key is None or key in state.open_agents:
                    errors.append("illegal_agent_transition")
                elif (
                    state.active_attempt_id != event.attempt_id
                    or state.open_phase is None
                    or event.phase != state.open_phase
                ):
                    errors.append("illegal_agent_transition")
                else:
                    state.open_agents.add(key)
            elif (
                key is None
                or key not in state.open_agents
                or state.open_phase is None
                or event.phase != state.open_phase
                or key in state.open_models.values()
                or any(
                    parent == key and state.tool_states.get(tool_key) in {"proposed", "started"}
                    for tool_key, parent in state.tool_parents.items()
                )
            ):
                errors.append("illegal_agent_transition")
            else:
                state.open_agents.remove(key)
        elif event_type in {
            RuntimeEventType.MODEL_STARTED,
            RuntimeEventType.MODEL_COMPLETED,
            RuntimeEventType.MODEL_FAILED,
        }:
            key = self._entity_key(event)
            parent = event.actor_name
            if event_type == RuntimeEventType.MODEL_STARTED:
                if key is None or key in state.open_models:
                    errors.append("illegal_model_transition")
                elif (
                    state.active_attempt_id != event.attempt_id
                    or state.open_phase is None
                    or event.phase != state.open_phase
                    or parent is None
                    or parent not in state.open_agents
                ):
                    errors.append("illegal_model_transition")
                else:
                    state.open_models[key] = parent
            elif (
                key is None
                or key not in state.open_models
                or state.open_phase is None
                or event.phase != state.open_phase
                or parent is None
                or state.open_models.get(key) != parent
                or parent not in state.open_agents
            ):
                errors.append("illegal_model_transition")
            else:
                state.open_models.pop(key)
        elif event_type.value.startswith("tool."):
            if (
                state.active_attempt_id != event.attempt_id
                or state.open_phase is None
                or event.phase != state.open_phase
            ):
                errors.append("illegal_tool_transition")
            else:
                errors.extend(self._advance_tool(state, event))
        elif event_type == RuntimeEventType.EVIDENCE_PERSISTED:
            if (
                not event.evidence_ids
                or state.active_attempt_id != event.attempt_id
                or state.open_phase is None
                or event.phase != state.open_phase
            ):
                errors.append("illegal_evidence_transition")
        elif event_type == RuntimeEventType.EVIDENCE_REJECTED:
            if (
                state.active_attempt_id != event.attempt_id
                or state.open_phase is None
                or event.phase != state.open_phase
            ):
                errors.append("illegal_evidence_transition")
        elif event_type == RuntimeEventType.RECOVERY_STARTED:
            if (
                state.run_status != "running"
                or state.active_attempt_id != event.attempt_id
                or state.recovery_attempt_id is not None
            ):
                errors.append("illegal_recovery_transition")
            state.recovery_attempt_id = event.attempt_id
            state.recovery_checkpoint_ids[event.attempt_id] = event.safe_payload.get(
                "checkpoint_id"
            )
            state.recovery_attempt_numbers[event.attempt_id] = event.safe_payload.get(
                "resume_attempt_number"
            )
        elif event_type == RuntimeEventType.RECOVERY_COMPLETED:
            if state.recovery_attempt_id != event.attempt_id:
                errors.append("illegal_recovery_transition")
            state.recovery_attempt_id = None
        elif event_type == RuntimeEventType.RECOVERY_REJECTED:
            if state.run_status != "interrupted" or state.active_attempt_id is not None:
                errors.append("illegal_recovery_transition")
        return errors

    @staticmethod
    def _advance_tool(state: _LifecycleState, event) -> list[str]:
        key = event.tool_call_id or event.safe_payload.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return ["illegal_tool_transition"]
        parent = event.actor_name
        if parent is None or parent not in state.open_agents:
            return ["illegal_tool_transition"]
        current = state.tool_states.get(key)
        if event.event_type == RuntimeEventType.TOOL_PROPOSED:
            if current is not None:
                return ["illegal_tool_transition"]
            state.tool_states[key] = "proposed"
            state.tool_parents[key] = parent
        elif event.event_type == RuntimeEventType.TOOL_STARTED:
            if current not in {None, "proposed"} or (
                current == "proposed" and state.tool_parents.get(key) != parent
            ):
                return ["illegal_tool_transition"]
            state.tool_states[key] = "started"
            state.tool_parents[key] = parent
        elif event.event_type in {
            RuntimeEventType.TOOL_COMPLETED,
            RuntimeEventType.TOOL_FAILED,
        }:
            if current != "started" or state.tool_parents.get(key) != parent:
                return ["illegal_tool_transition"]
            state.tool_states[key] = "terminal"
        elif event.event_type in {
            RuntimeEventType.TOOL_REJECTED,
            RuntimeEventType.TOOL_SKIPPED,
        }:
            if current != "proposed" or state.tool_parents.get(key) != parent:
                return ["illegal_tool_transition"]
            state.tool_states[key] = "terminal"
        return []

    @staticmethod
    def _has_open_work(state: _LifecycleState) -> bool:
        return bool(
            state.open_phase is not None
            or state.open_agents
            or state.open_models
            or state.recovery_attempt_id is not None
            or any(status in {"proposed", "started"} for status in state.tool_states.values())
        )

    @staticmethod
    def _has_open_phase_work(state: _LifecycleState) -> bool:
        return bool(
            state.open_agents
            or state.open_models
            or any(status in {"proposed", "started"} for status in state.tool_states.values())
        )

    @staticmethod
    def _close_interrupted_work(state: _LifecycleState, *, clear_attempt: bool = True) -> None:
        if clear_attempt:
            state.active_attempt_id = None
        state.open_phase = None
        state.open_agents.clear()
        state.open_models.clear()
        state.tool_states = {key: "terminal" for key in state.tool_states}
        state.tool_parents.clear()
        state.recovery_attempt_id = None

    def _validate_business_projection(self, source, projection) -> list[str]:
        if projection is None:
            return []
        errors: list[str] = []
        evidence = [item.to_domain() for item in projection.evidence]
        hypotheses = [item.to_domain() for item in projection.hypotheses]
        try:
            validate_hypotheses(evidence, hypotheses)
        except ValueError:
            errors.append("business_hypothesis_invalid")

        findings = [item.to_domain() for item in projection.findings]
        try:
            if any(item.investigation_id != source.investigation_id for item in findings):
                raise ValueError("finding investigation mismatch")
            validate_agent_semantics(evidence, findings, [])
        except ValueError:
            errors.append("business_finding_invalid")

        review = projection.review.to_domain() if projection.review else None
        if review is not None:
            try:
                if review.investigation_id != source.investigation_id:
                    raise ValueError("review investigation mismatch")
                validate_agent_semantics(
                    evidence,
                    findings,
                    review.candidates,
                    review.root_causes,
                )
            except ValueError:
                errors.append("business_review_invalid")

        report = projection.report.to_domain() if projection.report else None
        if report is not None:
            try:
                if report.investigation_id != source.investigation_id:
                    raise ValueError("report investigation mismatch")
                validate_hypotheses(evidence, report.hypotheses)
                if any(item not in projection.action_ids for item in report.action_ids):
                    raise ValueError("report action reference mismatch")
                if any(
                    item not in projection.verification_suggestion_ids
                    for item in report.verification_suggestion_ids
                ):
                    raise ValueError("report verification reference mismatch")
            except ValueError:
                errors.append("business_report_invalid")
        return errors

    def _validate_checkpoints(self, source, projection) -> list[str]:
        errors: list[str] = []
        checkpoints = self.dependencies.store.list_checkpoints(source.id)
        frozen_projections = (
            projection.checkpoint_projections if projection is not None else {}
        )
        if projection is not None and set(frozen_projections) != {
            item.id for item in checkpoints
        }:
            errors.append("checkpoint_projection_mismatch")
        for checkpoint in checkpoints:
            if checkpoint.schema_version != 1:
                errors.append("unsupported_schema")
                continue
            expected_digest = checkpoint_digest(
                run_id=checkpoint.run_id,
                attempt_id=checkpoint.attempt_id,
                completed_phase=checkpoint.completed_phase,
                resume_state=checkpoint.resume_state,
                schema_version=checkpoint.schema_version,
            )
            if expected_digest != checkpoint.state_digest:
                errors.append("checkpoint_digest_mismatch")
            if projection is not None:
                if frozen_projections.get(checkpoint.id) != checkpoint.projection_digest:
                    errors.append("checkpoint_projection_mismatch")
        return list(dict.fromkeys(errors))

    def _benchmark_evaluation(self, source, projection) -> tuple[str, str | None]:
        environment = projection.environment if projection is not None else None
        if environment != "openrca":
            return "not_applicable", None
        if self.dependencies.benchmark_evaluator is None:
            return "not_available", "benchmark_artifact_missing"
        try:
            return self.dependencies.benchmark_evaluator(source.id), None
        except OSError:
            return "not_available", "benchmark_artifact_missing"
        except (TypeError, ValueError):
            return "invalid", "benchmark_artifact_invalid"

    def _report(
        self,
        source,
        *,
        errors: list[str],
        benchmark: str = "not_applicable",
        external_call_count: int = 0,
    ) -> ReplayReport:
        replay_run = self.dependencies.store.create_run(
            RuntimeRun(
                investigation_id=source.investigation_id,
                run_kind=RuntimeRunKind.REPLAY,
                strategy=source.strategy,
                status=(RuntimeRunStatus.COMPLETED if not errors else RuntimeRunStatus.FAILED),
                source_run_id=source.id,
                run_reason=RuntimeRunReason.REPLAY,
                model_provider=source.model_provider,
                model_name=source.model_name,
                prompt_version=source.prompt_version,
                tool_budget=source.tool_budget,
                token_budget=source.token_budget,
                timeout_seconds=source.timeout_seconds,
                execution_contract_version=source.execution_contract_version,
                authority_mode=source.authority_mode,
                execution_contract=source.execution_contract,
                failure_category=(None if not errors else RuntimeFailureCategory.OUTPUT_VALIDATION),
                completed_at=datetime.now(UTC),
            )
        )
        return ReplayReport(
            replay_run_id=replay_run.id,
            source_run_id=source.id,
            valid=not errors,
            validation_errors=list(dict.fromkeys(errors)),
            benchmark_evaluation=benchmark,
            external_call_count=external_call_count,
        )
