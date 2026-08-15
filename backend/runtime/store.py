from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from backend.db.models import InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunStatus,
    ensure_attempt_transition,
    ensure_run_transition,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.runtime.faults import NoFaultInjector
from backend.safety.redaction import safe_failure

if TYPE_CHECKING:
    from backend.runtime.phases import PhaseCommit, ToolCommit
    from backend.runtime.telemetry import TraceReference

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeTerminalEvent:
    event_type: RuntimeEventType
    actor_type: RuntimeActorType
    phase: RuntimePhase | None = None
    safe_payload: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTerminalCommit:
    run_id: str
    attempt_id: str
    lease_owner: str
    lease_version: int
    expected_run_status: RuntimeRunStatus
    target_run_status: RuntimeRunStatus
    expected_attempt_status: RuntimeAttemptStatus
    target_attempt_status: RuntimeAttemptStatus
    events: tuple[RuntimeTerminalEvent, ...]
    failure_category: RuntimeFailureCategory | None = None


class RuntimeErrorBase(RuntimeError):
    """Runtime Store 对外暴露的稳定错误基类。"""


class RuntimeNotFound(RuntimeErrorBase):
    pass


class RuntimeConflict(RuntimeErrorBase):
    pass


class RuntimeLeaseLost(RuntimeConflict):
    pass


class RuntimeIntegrityError(RuntimeErrorBase):
    pass


class RuntimeContractError(RuntimeIntegrityError):
    """调用者提供的 Runtime 合同字段与服务端固定配置不一致。"""


def ensure_v11_tool_budget_available(
    run: RuntimeRun,
    existing_calls: list[ToolCallRecord],
    incoming: ToolCallRecord,
) -> None:
    """在 RUNNING durable commit 内原子保留一次 V11 tool attempt。"""
    if not run.is_v11 or incoming.status != ToolCallStatus.RUNNING:
        return
    if any(call.id == incoming.id for call in existing_calls):
        return
    if run.tool_budget is None:
        raise RuntimeIntegrityError("V11 run lacks frozen tool budget")
    consumed = {
        call.logical_call_id or call.id
        for call in existing_calls
        if call.runtime_run_id == run.id
        and call.status
        in {
            ToolCallStatus.RUNNING,
            ToolCallStatus.SUCCESS,
            ToolCallStatus.FAILED,
            ToolCallStatus.INTERRUPTED,
        }
    }
    if incoming.logical_call_id is not None and incoming.logical_call_id in consumed:
        # transport retry 复用同一逻辑动作的 durable reservation，不重复扣预算。
        return
    if len(consumed) >= run.tool_budget:
        raise RuntimeConflict("V11 tool budget exhausted")


class RuntimePersistenceError(RuntimeErrorBase):
    pass


def validate_v11_phase_ownership(
    run: RuntimeRun,
    commit,
    record,
    *,
    previous_run: RuntimeRun | None = None,
    previous_projection: dict | None = None,
) -> None:
    """在业务摘要计算前拒绝未激活、串 owner 或未冻结的 V11 提交。"""
    if not run.is_v11:
        return
    mutation = commit.business_mutation
    if commit.phase == RuntimePhase.INTAKE:
        if not mutation.activate_projection:
            raise RuntimeIntegrityError("V11 INTAKE must activate its projection")
        if (
            mutation.investigation is None
            or mutation.investigation.active_runtime_run_id != run.id
        ):
            raise RuntimeIntegrityError("V11 INTAKE activation owner mismatch")
        active_owner = record.active_runtime_run_id
        if active_owner is not None and active_owner != run.id:
            if previous_run is None or previous_run.status not in {
                RuntimeRunStatus.COMPLETED,
                RuntimeRunStatus.FAILED,
                RuntimeRunStatus.CANCELLED,
            }:
                raise RuntimeConflict("previous projection owner is not terminal")
            if previous_projection is None:
                raise RuntimeConflict("previous projection owner has no frozen view")
            from backend.runtime.diff import parse_frozen_projection

            try:
                parse_frozen_projection(previous_projection)
            except (TypeError, ValueError) as exc:
                raise RuntimeConflict("previous projection owner has invalid frozen view") from exc
    elif record.active_runtime_run_id != run.id:
        raise RuntimeIntegrityError("V11 business commit does not own active projection")

    owned_values = (
        [mutation.investigation, mutation.plan, mutation.review, mutation.react_trace]
        + list(mutation.tasks or ())
        + list(mutation.executions or ())
        + list(mutation.findings or ())
        + list(mutation.tool_calls or ())
    )
    for value in owned_values:
        if value is None:
            continue
        payloads = [value]
        for attribute in (
            "tasks",
            "critic_assessments",
            "evidence",
            "actions",
            "verification_suggestions",
        ):
            payloads.extend(getattr(value, attribute, ()) or ())
        payloads.extend(
            item
            for item in (
                getattr(value, "report", None),
                getattr(value, "multi_agent_run", None),
            )
            if item is not None
        )
        for payload in payloads:
            owner = getattr(payload, "active_runtime_run_id", None)
            if owner is None:
                owner = getattr(payload, "runtime_run_id", None)
            if owner != run.id:
                raise RuntimeIntegrityError("V11 business payload owner mismatch")


class RuntimeStore(Protocol):
    def create_run(self, run: RuntimeRun) -> RuntimeRun: ...

    def get_run(self, run_id: str) -> RuntimeRun: ...

    def list_runs(self, investigation_id: str) -> list[RuntimeRun]: ...

    def get_frozen_business_projection(self, run_id: str) -> dict | None: ...

    def set_benchmark_replay_locator(self, run_id: str, locator: dict) -> None: ...

    def get_benchmark_replay_locator(self, run_id: str) -> dict | None: ...

    def create_attempt(
        self, attempt: RuntimeAttempt, *, owner: str, lease_version: int
    ) -> RuntimeAttempt: ...

    def acquire_lease_and_create_attempt(
        self,
        run_id: str,
        *,
        attempt: RuntimeAttempt,
        owner: str,
        expected_status: RuntimeRunStatus,
    ) -> tuple[RuntimeRun, RuntimeAttempt]: ...

    def get_attempt(self, attempt_id: str) -> RuntimeAttempt: ...

    def list_attempts(self, run_id: str) -> list[RuntimeAttempt]: ...

    def set_attempt_trace_context(
        self, attempt_id: str, reference: TraceReference
    ) -> None: ...

    def get_attempt_trace_context(self, attempt_id: str) -> TraceReference | None: ...

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected: RuntimeAttemptStatus,
        target: RuntimeAttemptStatus,
        owner: str,
        lease_version: int,
    ) -> RuntimeAttempt: ...

    def renew_lease(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> RuntimeRun | None: ...

    def transition_run(
        self,
        run_id: str,
        *,
        expected: RuntimeRunStatus,
        target: RuntimeRunStatus,
        owner: str | None = None,
        lease_version: int | None = None,
    ) -> RuntimeRun: ...

    def request_cancel(self, run_id: str) -> RuntimeRun: ...

    def append_event_command(self, command) -> RuntimeEvent: ...

    def commit_terminal(self, commit: RuntimeTerminalCommit) -> RuntimeRun: ...

    def commit_tool(self, commit: ToolCommit) -> ToolCallRecord: ...

    def append_recovery_rejection(
        self,
        run_id: str,
        *,
        attempt_id: str,
        checkpoint_id: str | None,
        message: str | None = None,
    ) -> RuntimeEvent: ...

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> list[RuntimeEvent]: ...

    def get_checkpoint(self, checkpoint_id: str) -> RuntimeCheckpoint: ...

    def list_checkpoints(self, run_id: str) -> list[RuntimeCheckpoint]: ...

    def audit_expired_leases(self, now: datetime) -> list[RuntimeRun]: ...

    def commit_phase(self, commit: PhaseCommit) -> RuntimeCheckpoint: ...

    def reserve_model_turn(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> int: ...


_ACTIVE_LIVE_STATUSES = {
    RuntimeRunStatus.CREATED,
    RuntimeRunStatus.RUNNING,
    RuntimeRunStatus.CANCELLING,
    RuntimeRunStatus.INTERRUPTED,
}


class InMemoryRuntimeStore:
    def __init__(
        self,
        investigation_repository: InMemoryInvestigationRepository,
        *,
        lease_seconds: int = 30,
        failpoint: Callable[[str], None] | None = None,
        event_publisher: Callable[[list[RuntimeEvent]], None] | None = None,
        fault_injector=None,
    ) -> None:
        self.investigation_repository = investigation_repository
        # Runtime 与业务 Repository 共用一把锁，Phase commit 才能保持原子可见性。
        self._lock = investigation_repository.transaction_lock
        self.lease_seconds = lease_seconds
        self._failpoint = failpoint
        self._event_publisher = event_publisher
        self.fault_injector = fault_injector or NoFaultInjector()
        self._runs: dict[str, RuntimeRun] = {}
        self._attempts: dict[str, RuntimeAttempt] = {}
        self._attempt_trace_contexts: dict[str, tuple[str, str]] = {}
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._checkpoints: dict[str, RuntimeCheckpoint] = {}
        self._frozen_business_projections: dict[str, dict] = {}
        self._benchmark_replay_locators: dict[str, dict] = {}

    def create_run(self, run: RuntimeRun) -> RuntimeRun:
        with self._lock:
            if run.id in self._runs:
                raise RuntimeConflict(f"runtime run already exists: {run.id}")
            self._ensure_investigation_exists(run.investigation_id)
            self._validate_linked_run(run, run.parent_run_id, "parent")
            self._validate_linked_run(run, run.source_run_id, "source")
            if run.run_kind == RuntimeRunKind.LIVE and run.status in _ACTIVE_LIVE_STATUSES:
                if any(
                    item.investigation_id == run.investigation_id
                    and item.run_kind == RuntimeRunKind.LIVE
                    and item.status in _ACTIVE_LIVE_STATUSES
                    for item in self._runs.values()
                ):
                    raise RuntimeConflict("investigation already has an active live run")
            stored = run.model_copy(deep=True)
            self._runs[stored.id] = stored
            self._events[stored.id] = []
            if stored.status in {
                RuntimeRunStatus.COMPLETED,
                RuntimeRunStatus.FAILED,
                RuntimeRunStatus.CANCELLED,
            }:
                if stored.run_kind == RuntimeRunKind.REPLAY:
                    frozen = copy.deepcopy(
                        self._frozen_business_projections.get(stored.source_run_id)
                    )
                else:
                    from backend.runtime.diff import freeze_business_projection

                    frozen = freeze_business_projection(
                        self.investigation_repository,
                        stored.investigation_id,
                        runtime_run_id=(stored.id if stored.is_v11 else None),
                        authority_mode=(
                            stored.authority_mode.value if stored.is_v11 else None
                        ),
                    )
                if frozen is not None:
                    self._frozen_business_projections[stored.id] = frozen
            return stored.model_copy(deep=True)

    def get_run(self, run_id: str) -> RuntimeRun:
        with self._lock:
            try:
                return self._runs[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise RuntimeNotFound(f"unknown runtime run: {run_id}") from exc

    def list_runs(self, investigation_id: str) -> list[RuntimeRun]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    (
                        run
                        for run in self._runs.values()
                        if run.investigation_id == investigation_id
                    ),
                    key=lambda run: (run.created_at, run.id),
                    reverse=True,
                )
            ]

    def get_frozen_business_projection(self, run_id: str) -> dict | None:
        with self._lock:
            self._require_run(run_id)
            return copy.deepcopy(self._frozen_business_projections.get(run_id))

    def set_benchmark_replay_locator(self, run_id: str, locator: dict) -> None:
        with self._lock:
            self._require_run(run_id)
            existing = self._benchmark_replay_locators.get(run_id)
            if existing is not None and existing != locator:
                raise RuntimeConflict("benchmark replay locator is immutable")
            self._benchmark_replay_locators[run_id] = copy.deepcopy(locator)

    def get_benchmark_replay_locator(self, run_id: str) -> dict | None:
        with self._lock:
            self._require_run(run_id)
            return copy.deepcopy(self._benchmark_replay_locators.get(run_id))

    def create_attempt(
        self, attempt: RuntimeAttempt, *, owner: str, lease_version: int
    ) -> RuntimeAttempt:
        with self._lock:
            run = self._require_run(attempt.run_id)
            if run.status != RuntimeRunStatus.RUNNING:
                raise RuntimeConflict("attempt requires a running run")
            self._validate_fence(run, owner, lease_version)
            if attempt.id in self._attempts:
                raise RuntimeConflict(f"runtime attempt already exists: {attempt.id}")
            numbers = [
                item.attempt_number
                for item in self._attempts.values()
                if item.run_id == attempt.run_id
            ]
            if attempt.attempt_number != (max(numbers, default=0) + 1):
                raise RuntimeConflict("attempt_number must be consecutive per run")
            if any(
                item.run_id == attempt.run_id
                and item.status == RuntimeAttemptStatus.RUNNING
                for item in self._attempts.values()
            ):
                raise RuntimeConflict("run already has an active attempt")
            stored = attempt.model_copy(deep=True)
            self._attempts[stored.id] = stored
            return stored.model_copy(deep=True)

    def set_attempt_trace_context(self, attempt_id: str, reference) -> None:
        from backend.runtime.telemetry import TraceReference

        validated = TraceReference.model_validate(reference)
        with self._lock:
            if attempt_id not in self._attempts:
                raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
            self._attempt_trace_contexts[attempt_id] = (
                validated.trace_id,
                validated.root_span_id,
            )

    def get_attempt_trace_context(self, attempt_id: str):
        from backend.runtime.telemetry import TraceReference

        with self._lock:
            if attempt_id not in self._attempts:
                raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
            values = self._attempt_trace_contexts.get(attempt_id)
            if values is None:
                return None
            return TraceReference(trace_id=values[0], root_span_id=values[1])

    def acquire_lease_and_create_attempt(
        self,
        run_id: str,
        *,
        attempt: RuntimeAttempt,
        owner: str,
        expected_status: RuntimeRunStatus,
    ) -> tuple[RuntimeRun, RuntimeAttempt]:
        with self._lock:
            before = self._require_run(run_id).model_copy(deep=True)
            leased = self._acquire_lease(
                run_id, owner=owner, expected_status=expected_status
            )
            if leased is None:
                raise RuntimeConflict("runtime lease acquisition lost the race")
            try:
                self._hit_failpoint("after_lease_before_attempt")
                created = self.create_attempt(
                    attempt, owner=owner, lease_version=leased.lease_version
                )
            except (RuntimeConflict, RuntimeLeaseLost):
                self._runs[run_id] = before
                raise
            except Exception as exc:
                self._runs[run_id] = before
                raise RuntimePersistenceError(
                    "attempt start rolled back after lease acquisition"
                ) from exc
            return leased, created

    def get_attempt(self, attempt_id: str) -> RuntimeAttempt:
        with self._lock:
            try:
                return self._attempts[attempt_id].model_copy(deep=True)
            except KeyError as exc:
                raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}") from exc

    def list_attempts(self, run_id: str) -> list[RuntimeAttempt]:
        with self._lock:
            self._require_run(run_id)
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    (item for item in self._attempts.values() if item.run_id == run_id),
                    key=lambda item: item.attempt_number,
                )
            ]

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected: RuntimeAttemptStatus,
        target: RuntimeAttemptStatus,
        owner: str,
        lease_version: int,
    ) -> RuntimeAttempt:
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
            run = self._require_run(attempt.run_id)
            self._validate_fence(run, owner, lease_version)
            if attempt.status != expected:
                raise RuntimeConflict("runtime attempt status changed")
            try:
                ensure_attempt_transition(expected, target)
            except ValueError as exc:
                raise RuntimeConflict(str(exc)) from exc
            attempt.status = target
            attempt.completed_at = datetime.now(UTC)
            return attempt.model_copy(deep=True)

    def _acquire_lease(
        self, run_id: str, *, owner: str, expected_status: RuntimeRunStatus
    ) -> RuntimeRun | None:
        with self._lock:
            run = self._require_run(run_id)
            now = datetime.now(UTC)
            if run.status != expected_status:
                return None
            if run.lease_owner is not None and run.lease_expires_at is not None:
                if run.lease_expires_at > now:
                    return None
            if expected_status in {
                RuntimeRunStatus.CREATED,
                RuntimeRunStatus.INTERRUPTED,
            }:
                ensure_run_transition(expected_status, RuntimeRunStatus.RUNNING)
                run.status = RuntimeRunStatus.RUNNING
            else:
                return None
            run.lease_owner = owner
            run.lease_version += 1
            run.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            run.started_at = run.started_at or now
            return run.model_copy(deep=True)

    def renew_lease(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> RuntimeRun | None:
        with self._lock:
            run = self._require_run(run_id)
            now = datetime.now(UTC)
            if (
                run.status not in {RuntimeRunStatus.RUNNING, RuntimeRunStatus.CANCELLING}
                or run.lease_owner != owner
                or run.lease_version != lease_version
                or run.lease_expires_at is None
                or run.lease_expires_at <= now
            ):
                return None
            run.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            return run.model_copy(deep=True)

    def transition_run(
        self,
        run_id: str,
        *,
        expected: RuntimeRunStatus,
        target: RuntimeRunStatus,
        owner: str | None = None,
        lease_version: int | None = None,
    ) -> RuntimeRun:
        with self._lock:
            run = self._require_run(run_id)
            if run.status != expected:
                raise RuntimeConflict(
                    f"runtime run status is {run.status.value}, expected {expected.value}"
                )
            if expected in {
                RuntimeRunStatus.RUNNING,
                RuntimeRunStatus.CANCELLING,
                RuntimeRunStatus.INTERRUPTED,
            } and (owner is None or lease_version is None):
                raise RuntimeLeaseLost("active runtime transition requires a lease fence")
            self._validate_fence(run, owner, lease_version)
            try:
                ensure_run_transition(expected, target)
            except ValueError as exc:
                raise RuntimeConflict(str(exc)) from exc
            self._apply_transition(run, target)
            return run.model_copy(deep=True)

    def request_cancel(self, run_id: str) -> RuntimeRun:
        with self._lock:
            run = self._require_run(run_id)
            now = datetime.now(UTC)
            owns_projection = (
                not run.is_v11
                or self.investigation_repository.get(
                    run.investigation_id
                ).active_runtime_run_id
                == run.id
            )
            if run.status == RuntimeRunStatus.CREATED:
                from backend.runtime.diff import freeze_business_projection

                frozen_projection = freeze_business_projection(
                    self.investigation_repository,
                    run.investigation_id,
                    runtime_run_id=(run.id if run.is_v11 else None),
                    authority_mode=(
                        run.authority_mode.value if run.is_v11 else None
                    ),
                )
                run.cancel_requested_at = now
                self._apply_transition(run, RuntimeRunStatus.CANCELLED)
            elif run.status == RuntimeRunStatus.RUNNING:
                run.cancel_requested_at = now
                self._apply_transition(run, RuntimeRunStatus.CANCELLING)
            elif run.status != RuntimeRunStatus.CANCELLING:
                raise RuntimeConflict(f"run cannot be cancelled from {run.status.value}")
            if run.status == RuntimeRunStatus.CANCELLED:
                run.failure_category = RuntimeFailureCategory.CANCELLED
                if owns_projection:
                    self.investigation_repository.update_status(
                        run.investigation_id,
                        InvestigationStatus.CANCELLED,
                    )
                self._frozen_business_projections[run.id] = frozen_projection
            return run.model_copy(deep=True)

    def _append_event_for_test(self, event: RuntimeEvent) -> RuntimeEvent:
        """仅供 Store 合约测试构造历史；生产事件必须经 RuntimeWriter。"""
        with self._lock:
            event = RuntimeEvent.model_validate(event.model_dump(mode="python"))
            self._validate_event_scope(event)
            events = self._events[event.run_id]
            expected = len(events) + 1
            if event.sequence != expected:
                raise RuntimeConflict(
                    f"event sequence must be {expected}, got {event.sequence}"
                )
            stored = event.model_copy(deep=True)
            events.append(stored)
            return stored.model_copy(deep=True)

    def append_event_command(self, command) -> RuntimeEvent:
        published: list[RuntimeEvent] = []
        with self._lock:
            self.fault_injector.hit("unsafe_event_payload")
            run = self._require_run(command.run_id)
            if run.status not in {
                RuntimeRunStatus.RUNNING,
                RuntimeRunStatus.CANCELLING,
            }:
                raise RuntimeConflict("terminal or inactive run rejects runtime events")
            self._validate_fence(run, command.lease_owner, command.lease_version)
            attempt = self._attempts.get(command.attempt_id)
            if (
                attempt is None
                or attempt.run_id != command.run_id
                or attempt.status != RuntimeAttemptStatus.RUNNING
            ):
                raise RuntimeIntegrityError("event attempt is stale or belongs to another run")
            event = RuntimeEvent(
                run_id=command.run_id,
                attempt_id=command.attempt_id,
                sequence=len(self._events[command.run_id]) + 1,
                event_type=command.event_type,
                phase=command.phase,
                actor_type=command.actor_type,
                actor_name=command.actor_name,
                task_id=command.task_id,
                execution_id=command.execution_id,
                tool_call_id=command.tool_call_id,
                evidence_ids=list(command.evidence_ids),
                safe_payload=command.safe_payload or {},
                schema_version=command.schema_version,
            )
            event = RuntimeEvent.model_validate(event.model_dump(mode="python"))
            self._events[command.run_id].append(event)
            published = [event]
        self._publish_events(published)
        return event.model_copy(deep=True)

    def commit_terminal(self, commit: RuntimeTerminalCommit) -> RuntimeRun:
        """在同一临界区提交终态和审计事件，避免可恢复的半终态。"""
        published: list[RuntimeEvent] = []
        with self._lock:
            run = self._require_run(commit.run_id)
            attempt = self._attempts.get(commit.attempt_id)
            owns_projection = (
                not run.is_v11
                or self.investigation_repository.get(
                    run.investigation_id
                ).active_runtime_run_id
                == run.id
            )
            if run.status != commit.expected_run_status:
                raise RuntimeConflict("runtime terminal status conflict")
            self._validate_fence(run, commit.lease_owner, commit.lease_version)
            if (
                attempt is None
                or attempt.run_id != run.id
                or attempt.status != commit.expected_attempt_status
            ):
                raise RuntimeIntegrityError("runtime terminal attempt conflict")
            ensure_run_transition(run.status, commit.target_run_status)
            ensure_attempt_transition(attempt.status, commit.target_attempt_status)
            sequence = len(self._events[run.id])
            for item in commit.events:
                sequence += 1
                event = RuntimeEvent(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    sequence=sequence,
                    event_type=item.event_type,
                    phase=item.phase,
                    actor_type=item.actor_type,
                    safe_payload=item.safe_payload or {},
                )
                published.append(
                    RuntimeEvent.model_validate(event.model_dump(mode="python"))
                )
            from backend.runtime.diff import (
                finalize_frozen_projection,
                freeze_business_projection,
            )

            frozen_projection = finalize_frozen_projection(
                freeze_business_projection(
                    self.investigation_repository,
                    run.investigation_id,
                    runtime_run_id=(run.id if run.is_v11 else None),
                    authority_mode=(
                        run.authority_mode.value if run.is_v11 else None
                    ),
                ),
                (
                    checkpoint
                    for checkpoint in self._checkpoints.values()
                    if checkpoint.run_id == run.id
                ),
            )
            # 所有校验和事件构造成功后才修改内存状态。
            self.fault_injector.hit("persistence_mid_transaction")
            self._hit_failpoint("persistence_mid_transaction")
            if commit.target_run_status in {
                RuntimeRunStatus.COMPLETED,
                RuntimeRunStatus.FAILED,
                RuntimeRunStatus.CANCELLED,
            }:
                terminal_message = {
                    RuntimeRunStatus.COMPLETED: "runtime completed",
                    RuntimeRunStatus.FAILED: "runtime failed",
                    RuntimeRunStatus.CANCELLED: "runtime cancelled",
                }[commit.target_run_status]
                interrupted_calls = [
                    call.model_copy(
                        update={
                            "status": ToolCallStatus.INTERRUPTED,
                            "completed_at": datetime.now(UTC),
                            "error_message": terminal_message,
                        }
                    )
                    for call in self.investigation_repository.list_tool_calls(
                        run.investigation_id
                    )
                    if call.runtime_run_id == run.id
                    and call.status
                    in {ToolCallStatus.PENDING, ToolCallStatus.RUNNING}
                ]
                if interrupted_calls:
                    self.investigation_repository.save_tool_calls(
                        run.investigation_id, interrupted_calls
                    )
            self._events[run.id].extend(published)
            run.failure_category = commit.failure_category
            attempt.failure_category = commit.failure_category
            self._apply_transition(run, commit.target_run_status)
            investigation_status = {
                RuntimeRunStatus.COMPLETED: InvestigationStatus.COMPLETED,
                RuntimeRunStatus.FAILED: InvestigationStatus.FAILED,
                RuntimeRunStatus.CANCELLED: InvestigationStatus.CANCELLED,
            }.get(commit.target_run_status)
            if investigation_status is not None and owns_projection:
                self.investigation_repository.update_status(
                    run.investigation_id,
                    investigation_status,
                    failure_reason=(
                        safe_failure(commit.failure_category.value)
                        if investigation_status == InvestigationStatus.FAILED
                        and commit.failure_category is not None
                        else None
                    ),
                )
            self._frozen_business_projections[run.id] = frozen_projection
            result = run.model_copy(deep=True)
        self._publish_events(published)
        return result

    def commit_tool(self, commit) -> ToolCallRecord:
        """以独立 durable 单元提交 ToolCall 与其证据，供 checkpoint 前恢复复用。"""
        published: list[RuntimeEvent] = []
        with self._lock:
            run = self._require_run(commit.run_id)
            if run.status != RuntimeRunStatus.RUNNING:
                raise RuntimeConflict("tool commit requires running runtime")
            if run.is_v11:
                record = self.investigation_repository.get(run.investigation_id)
                if record.active_runtime_run_id != run.id:
                    raise RuntimeIntegrityError(
                        "V11 tool commit does not own active projection"
                    )
                if getattr(commit.call, "runtime_run_id", None) != run.id:
                    raise RuntimeIntegrityError("V11 tool payload owner mismatch")
                validate_v11_phase_ownership(run, commit, record)
            self._validate_fence(run, commit.lease_owner, commit.lease_version)
            attempt = self._attempts.get(commit.attempt_id)
            if (
                attempt is None
                or attempt.run_id != run.id
                or attempt.status != RuntimeAttemptStatus.RUNNING
            ):
                raise RuntimeIntegrityError("tool commit attempt is stale")
            existing_call = next(
                (
                    call
                    for call in self.investigation_repository.list_tool_calls(
                        run.investigation_id
                    )
                    if call.id == commit.call.id
                ),
                None,
            )
            ensure_v11_tool_budget_available(
                run,
                self.investigation_repository.list_tool_calls(run.investigation_id),
                commit.call,
            )
            if existing_call is not None and existing_call.status in {
                ToolCallStatus.SUCCESS,
                ToolCallStatus.FAILED,
                ToolCallStatus.INTERRUPTED,
            } and existing_call.status != commit.call.status:
                raise RuntimeConflict("terminal tool call rejects a late result")
            repository_attributes = (
                "_records",
                "_plans",
                "_tasks",
                "_executions",
                "_context_facts",
                "_tool_calls",
                "_memory_items",
                "_agent_findings",
                "_coordination_reviews",
                "_react_traces",
            )
            snapshot = {
                name: copy.deepcopy(getattr(self.investigation_repository, name))
                for name in repository_attributes
            }
            try:
                commit.business_mutation.apply_memory(self.investigation_repository)
                event_type = {
                    ToolCallStatus.RUNNING: RuntimeEventType.TOOL_STARTED,
                    ToolCallStatus.SUCCESS: RuntimeEventType.TOOL_COMPLETED,
                }.get(commit.call.status, RuntimeEventType.TOOL_FAILED)
                event = RuntimeEvent(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    sequence=len(self._events[run.id]) + 1,
                    event_type=event_type,
                    actor_type=RuntimeActorType.TOOL,
                    phase=commit.phase,
                    actor_name=commit.call.agent_name,
                    task_id=commit.call.task_id,
                    execution_id=commit.call.execution_id,
                    tool_call_id=commit.call.id,
                    evidence_ids=commit.call.output_evidence_ids,
                    safe_payload={
                        "status": commit.call.status.value,
                        "tool_name": commit.call.tool_name,
                        "idempotency_key": commit.call.idempotency_key,
                        "normalized_inputs": commit.call.input,
                    },
                )
                event = RuntimeEvent.model_validate(event.model_dump(mode="python"))
                self._events[run.id].append(event)
                published = [event]
            except Exception:
                for name, value in snapshot.items():
                    setattr(self.investigation_repository, name, value)
                raise
        self._publish_events(published)
        return commit.call.model_copy(deep=True)

    def append_recovery_rejection(
        self,
        run_id: str,
        *,
        attempt_id: str,
        checkpoint_id: str | None,
        message: str | None = None,
    ) -> RuntimeEvent:
        """在无活跃 lease 时记录恢复拒绝；仅允许引用最后一个 interrupted Attempt。"""
        published: list[RuntimeEvent] = []
        with self._lock:
            run = self._require_run(run_id)
            attempt = self._attempts.get(attempt_id)
            if run.status != RuntimeRunStatus.INTERRUPTED:
                raise RuntimeConflict("recovery rejection requires interrupted run")
            if (
                attempt is None
                or attempt.run_id != run_id
                or attempt.status != RuntimeAttemptStatus.INTERRUPTED
            ):
                raise RuntimeIntegrityError(
                    "recovery rejection attempt is not interrupted"
                )
            if run.is_v11:
                from backend.runtime.diff import freeze_business_projection

                self._frozen_business_projections[run.id] = (
                    freeze_business_projection(
                        self.investigation_repository,
                        run.investigation_id,
                        runtime_run_id=run.id,
                        authority_mode=run.authority_mode.value,
                    )
                )
            event = RuntimeEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=len(self._events[run_id]) + 1,
                event_type=RuntimeEventType.RECOVERY_REJECTED,
                actor_type=RuntimeActorType.RECOVERY,
                safe_payload={
                    "status": "rejected",
                    "failure_category": "checkpoint_invalid",
                    **(
                        {"checkpoint_id": checkpoint_id}
                        if checkpoint_id is not None
                        else {}
                    ),
                    **({"message": message} if message is not None else {}),
                },
            )
            self._events[run_id].append(event)
            published = [event]
        self._publish_events(published)
        return event.model_copy(deep=True)

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> list[RuntimeEvent]:
        with self._lock:
            self._require_run(run_id)
            if limit < 1:
                raise ValueError("limit must be positive")
            return [
                event.model_copy(deep=True)
                for event in self._events[run_id]
                if event.sequence > after
            ][:limit]

    def get_checkpoint(self, checkpoint_id: str) -> RuntimeCheckpoint:
        with self._lock:
            try:
                return self._checkpoints[checkpoint_id].model_copy(deep=True)
            except KeyError as exc:
                raise RuntimeNotFound(f"unknown runtime checkpoint: {checkpoint_id}") from exc

    def list_checkpoints(self, run_id: str) -> list[RuntimeCheckpoint]:
        with self._lock:
            self._require_run(run_id)
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    (
                        checkpoint
                        for checkpoint in self._checkpoints.values()
                        if checkpoint.run_id == run_id
                    ),
                    key=lambda checkpoint: checkpoint.event_sequence,
                )
            ]

    def audit_expired_leases(self, now: datetime) -> list[RuntimeRun]:
        published: list[RuntimeEvent] = []
        with self._lock:
            interrupted: list[RuntimeRun] = []
            for run in self._runs.values():
                if (
                    run.status in {RuntimeRunStatus.RUNNING, RuntimeRunStatus.CANCELLING}
                    and run.lease_expires_at is not None
                    and run.lease_expires_at < now
                ):
                    attempt = next(
                        (
                            item
                            for item in self._attempts.values()
                            if item.run_id == run.id
                            and item.status == RuntimeAttemptStatus.RUNNING
                        ),
                        None,
                    )
                    if attempt is None:
                        raise RuntimeIntegrityError(
                            "expired runtime run has no active attempt"
                        )
                    nonterminal_calls = [
                        call.model_copy(
                            update={
                                "status": ToolCallStatus.INTERRUPTED,
                                "completed_at": now,
                                "error_message": "runtime lease expired",
                            }
                        )
                        for call in self.investigation_repository.list_tool_calls(
                            run.investigation_id
                        )
                        if call.runtime_run_id == run.id
                        and call.status
                        in {
                            ToolCallStatus.PENDING,
                            ToolCallStatus.RUNNING,
                        }
                    ]
                    if nonterminal_calls:
                        self.investigation_repository.save_tool_calls(
                            run.investigation_id, nonterminal_calls
                        )
                    run.failure_category = RuntimeFailureCategory.LEASE_LOST
                    attempt.failure_category = RuntimeFailureCategory.LEASE_LOST
                    self._apply_transition(run, RuntimeRunStatus.INTERRUPTED)
                    if run.is_v11:
                        from backend.runtime.diff import freeze_business_projection

                        self._frozen_business_projections[run.id] = (
                            freeze_business_projection(
                                self.investigation_repository,
                                run.investigation_id,
                                runtime_run_id=run.id,
                                authority_mode=run.authority_mode.value,
                            )
                        )
                    first_sequence = len(self._events[run.id]) + 1
                    run_event = RuntimeEvent(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        sequence=first_sequence,
                        event_type=RuntimeEventType.RUN_INTERRUPTED,
                        actor_type=RuntimeActorType.RUNTIME,
                        safe_payload={
                            "status": "interrupted",
                            "failure_category": "lease_lost",
                        },
                        occurred_at=now,
                    )
                    attempt_event = RuntimeEvent(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        sequence=first_sequence + 1,
                        event_type=RuntimeEventType.ATTEMPT_INTERRUPTED,
                        actor_type=RuntimeActorType.RUNTIME,
                        safe_payload={
                            "status": "interrupted",
                            "failure_category": "lease_lost",
                        },
                        occurred_at=now,
                    )
                    self._events[run.id].extend((run_event, attempt_event))
                    published.extend((run_event, attempt_event))
                    interrupted.append(run.model_copy(deep=True))
        self._publish_events(published)
        return interrupted

    def reserve_model_turn(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> int:
        """在同一 Store 锁内原子扣减 V11 Run 的模型 turn 总预算。"""
        with self._lock:
            run = self._require_run(run_id)
            if not run.is_v11:
                raise RuntimeIntegrityError("model turn budget is only valid for V11")
            if run.status not in {
                RuntimeRunStatus.RUNNING,
                RuntimeRunStatus.CANCELLING,
            }:
                raise RuntimeConflict("model turn reservation requires a live run")
            self._validate_fence(run, owner, lease_version)
            remaining = run.remaining_model_turns
            if remaining is None:
                raise RuntimeIntegrityError("V11 run lacks durable model turn budget")
            if remaining <= 0:
                raise RuntimeConflict("V11 model turn budget exhausted")
            run.remaining_model_turns = remaining - 1
            return run.remaining_model_turns

    def commit_phase(self, commit: PhaseCommit) -> RuntimeCheckpoint:
        from backend.runtime.phases import (
            checkpoint_digest,
            durable_projection_digest,
            durable_token_usage,
            durable_tool_call_count,
            ensure_phase_precondition,
            phase_profile_for,
        )

        published: list[RuntimeEvent] = []
        with self._lock:
            run = self._require_run(commit.run_id)
            record = self.investigation_repository.get(run.investigation_id)
            previous_owner = record.active_runtime_run_id
            validate_v11_phase_ownership(
                run,
                commit,
                record,
                previous_run=(
                    self._runs.get(previous_owner)
                    if previous_owner is not None and previous_owner != run.id
                    else None
                ),
                previous_projection=(
                    self._frozen_business_projections.get(previous_owner)
                    if previous_owner is not None and previous_owner != run.id
                    else None
                ),
            )
            digest = checkpoint_digest(
                run_id=commit.run_id,
                attempt_id=commit.attempt_id,
                completed_phase=commit.phase,
                resume_state=commit.resume_state,
                schema_version=commit.schema_version,
            )
            try:
                projection_digest = durable_projection_digest(
                    repository=self.investigation_repository,
                    investigation_id=run.investigation_id,
                    run_id=run.id,
                    resume_state=commit.resume_state,
                    require_exact=True,
                    mutation=commit.business_mutation,
                )
                consumed_tools = durable_tool_call_count(
                    repository=self.investigation_repository,
                    investigation_id=run.investigation_id,
                    run_id=run.id,
                    mutation=commit.business_mutation,
                )
                consumed_tokens = durable_token_usage(
                    self._events[run.id], run.id
                )
            except ValueError as exc:
                raise RuntimeIntegrityError(str(exc)) from exc
            if run.tool_budget is not None and commit.resume_state.remaining_tool_budget != max(
                0, run.tool_budget - consumed_tools
            ):
                raise RuntimeIntegrityError(
                    "checkpoint remaining tool budget differs from durable consumption"
                )
            if (
                run.token_budget is not None
                and commit.resume_state.remaining_token_budget
                != max(0, run.token_budget - consumed_tokens)
            ):
                raise RuntimeIntegrityError(
                    "checkpoint remaining token budget differs from durable consumption"
                )
            if run.is_v11 and "execution_contract_digest" in run.execution_contract and (
                commit.resume_state.remaining_model_turns
                != run.remaining_model_turns
            ):
                raise RuntimeIntegrityError(
                    "checkpoint remaining model turns differ from durable run budget"
                )
            existing = self._checkpoints.get(commit.checkpoint_id)
            if existing is not None:
                if (
                    existing.run_id == commit.run_id
                    and existing.attempt_id == commit.attempt_id
                    and existing.completed_phase == commit.phase
                    and existing.state_digest == digest
                    and existing.projection_digest == projection_digest
                ):
                    return existing.model_copy(deep=True)
                raise RuntimeConflict("checkpoint id was reused for another phase result")
            if run.status not in {
                RuntimeRunStatus.RUNNING,
                RuntimeRunStatus.CANCELLING,
            }:
                raise RuntimeConflict("phase result requires a running or cancelling run")
            self._validate_fence(run, commit.lease_owner, commit.lease_version)
            attempt = self._attempts.get(commit.attempt_id)
            if (
                attempt is None
                or attempt.run_id != commit.run_id
                or attempt.status != RuntimeAttemptStatus.RUNNING
            ):
                raise RuntimeIntegrityError("phase attempt does not belong to run")
            if run.investigation_id != commit.business_mutation.investigation_id:
                raise RuntimeIntegrityError("phase business mutation crosses investigation")
            try:
                ensure_phase_precondition(
                    current_phase=run.current_phase,
                    latest_checkpoint_id=run.latest_checkpoint_id,
                    commit=commit,
                    phase_order=phase_profile_for(run.execution_contract_version).order,
                )
            except ValueError as exc:
                raise RuntimeConflict(str(exc)) from exc

            repository_attributes = (
                "_records",
                "_plans",
                "_tasks",
                "_executions",
                "_context_facts",
                "_tool_calls",
                "_memory_items",
                "_agent_findings",
                "_coordination_reviews",
                "_react_traces",
            )
            repository_snapshot = {
                name: copy.deepcopy(getattr(self.investigation_repository, name))
                for name in repository_attributes
            }
            runtime_snapshot = (
                run.model_copy(deep=True),
                list(self._events[run.id]),
                dict(self._checkpoints),
            )
            try:
                commit.business_mutation.apply_memory(self.investigation_repository)
                self.fault_injector.hit("persistence_mid_transaction")
                self._hit_failpoint("after_business_before_event")
                first_sequence = len(self._events[run.id]) + 1
                phase_event = RuntimeEvent(
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    sequence=first_sequence,
                    event_type=(
                        RuntimeEventType.PHASE_SKIPPED
                        if commit.status == "skipped"
                        else RuntimeEventType.PHASE_FAILED
                        if commit.status == "failed"
                        else RuntimeEventType.PHASE_COMPLETED
                    ),
                    phase=commit.phase,
                    actor_type=RuntimeActorType.PHASE,
                    evidence_ids=commit.resume_state.completed_evidence_ids,
                    safe_payload=commit.safe_payload,
                    schema_version=commit.schema_version,
                )
                checkpoint_event = RuntimeEvent(
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    sequence=first_sequence + 1,
                    event_type=RuntimeEventType.CHECKPOINT_CREATED,
                    phase=commit.phase,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "checkpoint_id": commit.checkpoint_id,
                        "event_sequence": first_sequence + 1,
                        "state_digest": digest,
                        "projection_digest": projection_digest,
                    },
                    schema_version=commit.schema_version,
                )
                checkpoint = RuntimeCheckpoint(
                    id=commit.checkpoint_id,
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    completed_phase=commit.phase,
                    event_sequence=first_sequence + 1,
                    state_digest=digest,
                    projection_digest=projection_digest,
                    resume_state=commit.resume_state,
                    schema_version=commit.schema_version,
                )
                self._events[run.id].extend((phase_event, checkpoint_event))
                self._checkpoints[checkpoint.id] = checkpoint
                run.current_phase = commit.phase
                run.latest_checkpoint_id = checkpoint.id
                published = [phase_event, checkpoint_event]
            except RuntimeLeaseLost:
                self._restore_memory_phase(
                    run.id, repository_snapshot, runtime_snapshot
                )
                raise
            except Exception as exc:
                self._restore_memory_phase(
                    run.id, repository_snapshot, runtime_snapshot
                )
                raise RuntimePersistenceError("memory phase commit rolled back") from exc
        self._publish_events(published)
        return checkpoint.model_copy(deep=True)

    def _require_run(self, run_id: str) -> RuntimeRun:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RuntimeNotFound(f"unknown runtime run: {run_id}") from exc

    def _restore_memory_phase(
        self,
        run_id: str,
        repository_snapshot: dict[str, object],
        runtime_snapshot: tuple[
            RuntimeRun, list[RuntimeEvent], dict[str, RuntimeCheckpoint]
        ],
    ) -> None:
        for name, value in repository_snapshot.items():
            setattr(self.investigation_repository, name, value)
        run, events, checkpoints = runtime_snapshot
        self._runs[run_id] = run
        self._events[run_id] = events
        self._checkpoints = checkpoints

    def _hit_failpoint(self, point: str) -> None:
        if self._failpoint is not None:
            self._failpoint(point)

    def _publish_events(self, events: list[RuntimeEvent]) -> None:
        if self._event_publisher is None:
            return
        try:
            self._event_publisher([event.model_copy(deep=True) for event in events])
        except Exception as exc:
            # live feed 是提交后的优化路径，失败不能反向改变持久化事实。
            logger.warning("runtime event publication failed error_type=%s", type(exc).__name__)

    def _ensure_investigation_exists(self, investigation_id: str) -> None:
        try:
            self.investigation_repository.get(investigation_id)
        except ValueError as exc:
            raise RuntimeIntegrityError(
                f"unknown investigation: {investigation_id}"
            ) from exc

    def _validate_linked_run(
        self, run: RuntimeRun, linked_run_id: str | None, relation: str
    ) -> None:
        if linked_run_id is None:
            return
        linked = self._require_run(linked_run_id)
        if linked.investigation_id != run.investigation_id:
            linked_source = self.investigation_repository.get(
                run.investigation_id
            ).source_investigation_id
            if not (
                relation == "parent"
                and run.is_v11
                and linked_source == linked.investigation_id
            ):
                raise RuntimeConflict(f"{relation} run belongs to another investigation")
        if relation == "parent" and (
            linked.run_kind != RuntimeRunKind.LIVE
            or linked.status
            not in {
                RuntimeRunStatus.COMPLETED,
                RuntimeRunStatus.FAILED,
                RuntimeRunStatus.CANCELLED,
            }
        ):
            raise RuntimeConflict(f"{relation} run must be a terminal live run")

    def _validate_event_scope(self, event: RuntimeEvent) -> None:
        self._require_run(event.run_id)
        attempt = self._attempts.get(event.attempt_id)
        if attempt is None or attempt.run_id != event.run_id:
            raise RuntimeIntegrityError("event attempt does not belong to run")

    def _validate_fence(
        self, run: RuntimeRun, owner: str | None, lease_version: int | None
    ) -> None:
        if owner is None and lease_version is None:
            return
        now = datetime.now(UTC)
        if (
            owner is None
            or lease_version is None
            or run.lease_owner != owner
            or run.lease_version != lease_version
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise RuntimeLeaseLost("runtime lease fence rejected the write")

    def _apply_transition(self, run: RuntimeRun, target: RuntimeRunStatus) -> None:
        ensure_run_transition(run.status, target)
        run.status = target
        if target in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        }:
            run.completed_at = datetime.now(UTC)
        if target in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
            RuntimeRunStatus.INTERRUPTED,
        }:
            run.lease_owner = None
            run.lease_expires_at = None
        attempt_target = {
            RuntimeRunStatus.COMPLETED: RuntimeAttemptStatus.COMPLETED,
            RuntimeRunStatus.FAILED: RuntimeAttemptStatus.FAILED,
            RuntimeRunStatus.CANCELLED: RuntimeAttemptStatus.CANCELLED,
            RuntimeRunStatus.INTERRUPTED: RuntimeAttemptStatus.INTERRUPTED,
        }.get(target)
        if attempt_target is not None:
            for attempt in self._attempts.values():
                if (
                    attempt.run_id == run.id
                    and attempt.status == RuntimeAttemptStatus.RUNNING
                ):
                    attempt.status = attempt_target
                    attempt.completed_at = datetime.now(UTC)
