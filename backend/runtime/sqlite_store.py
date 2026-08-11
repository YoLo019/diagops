from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock

from pydantic import ValidationError
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.db.models import InvestigationStatus
from backend.db.schema import (
    investigations,
    runtime_attempts,
    runtime_checkpoints,
    runtime_events,
    runtime_runs,
    tool_calls,
)
from backend.db.sqlite_repository import SQLiteInvestigationRepository
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
    validate_v11_execution_contract,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.runtime.diff import freeze_business_projection
from backend.runtime.faults import NoFaultInjector, RuntimeInjectedFault
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeIntegrityError,
    RuntimeLeaseLost,
    RuntimeNotFound,
    RuntimePersistenceError,
    RuntimeTerminalCommit,
    ensure_v11_tool_budget_available,
    validate_v11_phase_ownership,
)
from backend.safety.redaction import safe_failure

logger = logging.getLogger(__name__)


class SQLiteRuntimeStore:
    def __init__(
        self,
        engine: Engine,
        investigation_repository: SQLiteInvestigationRepository,
        *,
        lease_seconds: int = 30,
        failpoint: Callable[[str], None] | None = None,
        event_publisher: Callable[[list[RuntimeEvent]], None] | None = None,
        fault_injector=None,
    ) -> None:
        self.engine = engine
        self.investigation_repository = investigation_repository
        self.lease_seconds = lease_seconds
        self._failpoint = failpoint
        self._event_publisher = event_publisher
        self.fault_injector = fault_injector or NoFaultInjector()
        # V9 是单进程执行器；该锁补足绕过 RuntimeWriter 的同进程直接 Store 调用。
        self._write_lock = RLock()

    def create_run(self, run: RuntimeRun) -> RuntimeRun:
        values = run.model_dump(mode="json")
        values["next_event_sequence"] = 0
        terminal = run.status in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        }
        if terminal and run.run_kind == RuntimeRunKind.REPLAY:
            values["frozen_business_projection"] = (
                self.get_frozen_business_projection(run.source_run_id)
            )
        elif terminal:
            values["frozen_business_projection"] = freeze_business_projection(
                self.investigation_repository,
                run.investigation_id,
                runtime_run_id=(run.id if run.is_v11 else None),
                authority_mode=(run.authority_mode.value if run.is_v11 else None),
            )
        else:
            values["frozen_business_projection"] = None
        values["benchmark_replay_locator"] = None
        try:
            with self.engine.begin() as connection:
                self._validate_linked_run(connection, run, run.parent_run_id, "parent")
                self._validate_linked_run(connection, run, run.source_run_id, "source")
                connection.execute(insert(runtime_runs).values(values))
        except IntegrityError as exc:
            raise RuntimeConflict("runtime run violates ownership or active-run rules") from exc
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to create runtime run") from exc
        return self.get_run(run.id)

    def get_run(self, run_id: str) -> RuntimeRun:
        with self.engine.begin() as connection:
            row = connection.execute(
                select(runtime_runs).where(runtime_runs.c.id == run_id)
            ).mappings().one_or_none()
            if row is None:
                raise RuntimeNotFound(f"unknown runtime run: {run_id}")
            return self._run_from_row(row, connection)

    def list_runs(self, investigation_id: str) -> list[RuntimeRun]:
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(runtime_runs)
                .where(runtime_runs.c.investigation_id == investigation_id)
                .order_by(runtime_runs.c.created_at.desc(), runtime_runs.c.id.desc())
            ).mappings().all()
            return [self._run_from_row(row, connection) for row in rows]

    def get_frozen_business_projection(self, run_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(runtime_runs.c.frozen_business_projection).where(
                    runtime_runs.c.id == run_id
                )
            ).scalar_one_or_none()
        if row is None:
            self.get_run(run_id)
            return None
        return dict(row)

    def set_benchmark_replay_locator(self, run_id: str, locator: dict) -> None:
        try:
            with self.engine.begin() as connection:
                existing = connection.execute(
                    select(runtime_runs.c.benchmark_replay_locator).where(
                        runtime_runs.c.id == run_id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    if connection.execute(
                        select(runtime_runs.c.id).where(runtime_runs.c.id == run_id)
                    ).scalar_one_or_none() is None:
                        raise RuntimeNotFound(f"unknown runtime run: {run_id}")
                    connection.execute(
                        update(runtime_runs)
                        .where(runtime_runs.c.id == run_id)
                        .values(benchmark_replay_locator=locator)
                    )
                elif dict(existing) != locator:
                    raise RuntimeConflict("benchmark replay locator is immutable")
        except (RuntimeNotFound, RuntimeConflict):
            raise
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError(
                "failed to persist benchmark replay locator"
            ) from exc

    def get_benchmark_replay_locator(self, run_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(runtime_runs.c.benchmark_replay_locator).where(
                    runtime_runs.c.id == run_id
                )
            ).scalar_one_or_none()
        if row is None:
            self.get_run(run_id)
            return None
        return dict(row)

    def create_attempt(
        self, attempt: RuntimeAttempt, *, owner: str, lease_version: int
    ) -> RuntimeAttempt:
        try:
            with self.engine.begin() as connection:
                self._insert_attempt(
                    connection,
                    attempt,
                    owner=owner,
                    lease_version=lease_version,
                )
        except (RuntimeNotFound, RuntimeConflict):
            raise
        except IntegrityError as exc:
            raise RuntimeConflict("runtime attempt conflicts with persisted history") from exc
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to create runtime attempt") from exc
        return attempt.model_copy(deep=True)

    def acquire_lease_and_create_attempt(
        self,
        run_id: str,
        *,
        attempt: RuntimeAttempt,
        owner: str,
        expected_status: RuntimeRunStatus,
    ) -> tuple[RuntimeRun, RuntimeAttempt]:
        if attempt.run_id != run_id:
            raise RuntimeIntegrityError("attempt does not belong to requested run")
        now = datetime.now(UTC)
        if expected_status not in {
            RuntimeRunStatus.CREATED,
            RuntimeRunStatus.INTERRUPTED,
        }:
            raise RuntimeConflict("attempt start requires created or interrupted run")
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == run_id,
                        runtime_runs.c.status == expected_status.value,
                        or_(
                            runtime_runs.c.lease_owner.is_(None),
                            runtime_runs.c.lease_expires_at.is_(None),
                            runtime_runs.c.lease_expires_at <= now.isoformat(),
                        ),
                    )
                    .values(
                        status=RuntimeRunStatus.RUNNING.value,
                        lease_owner=owner,
                        lease_expires_at=(
                            now + timedelta(seconds=self.lease_seconds)
                        ).isoformat(),
                        lease_version=runtime_runs.c.lease_version + 1,
                        started_at=func.coalesce(
                            runtime_runs.c.started_at, now.isoformat()
                        ),
                    )
                    .returning(runtime_runs)
                ).mappings().one_or_none()
                if row is None:
                    raise RuntimeConflict("runtime lease acquisition lost the race")
                run = self._run_from_row(row, connection)
                self._hit_failpoint("after_lease_before_attempt")
                self._insert_attempt(
                    connection,
                    attempt,
                    owner=owner,
                    lease_version=run.lease_version,
                )
        except (RuntimeConflict, RuntimeIntegrityError):
            raise
        except IntegrityError as exc:
            raise RuntimeConflict("run already has an active attempt") from exc
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to start runtime attempt") from exc
        except Exception as exc:
            raise RuntimePersistenceError(
                "attempt start rolled back after lease acquisition"
            ) from exc
        return run, attempt.model_copy(deep=True)

    def get_attempt(self, attempt_id: str) -> RuntimeAttempt:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(runtime_attempts).where(runtime_attempts.c.id == attempt_id)
            ).mappings().one_or_none()
        if row is None:
            raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
        return self._attempt_from_row(row)

    def set_attempt_trace_context(self, attempt_id: str, reference) -> None:
        from backend.runtime.telemetry import TraceReference

        validated = TraceReference.model_validate(reference)
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(runtime_attempts)
                    .where(runtime_attempts.c.id == attempt_id)
                    .values(
                        trace_id=validated.trace_id,
                        root_span_id=validated.root_span_id,
                    )
                )
                if result.rowcount != 1:
                    raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
        except RuntimeNotFound:
            raise
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError(
                "failed to persist attempt trace context"
            ) from exc

    def get_attempt_trace_context(self, attempt_id: str):
        from backend.runtime.telemetry import TraceReference

        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(
                        runtime_attempts.c.trace_id,
                        runtime_attempts.c.root_span_id,
                    ).where(runtime_attempts.c.id == attempt_id)
                ).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to load attempt trace context") from exc
        if row is None:
            raise RuntimeNotFound(f"unknown runtime attempt: {attempt_id}")
        if row["trace_id"] is None and row["root_span_id"] is None:
            return None
        if row["trace_id"] is None or row["root_span_id"] is None:
            raise RuntimeIntegrityError("attempt trace context is incomplete")
        return TraceReference(
            trace_id=row["trace_id"],
            root_span_id=row["root_span_id"],
        )

    def list_attempts(self, run_id: str) -> list[RuntimeAttempt]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(runtime_attempts)
                .where(runtime_attempts.c.run_id == run_id)
                .order_by(runtime_attempts.c.attempt_number)
            ).mappings().all()
        return [self._attempt_from_row(row) for row in rows]

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected: RuntimeAttemptStatus,
        target: RuntimeAttemptStatus,
        owner: str,
        lease_version: int,
    ) -> RuntimeAttempt:
        try:
            ensure_attempt_transition(expected, target)
        except ValueError as exc:
            raise RuntimeConflict(str(exc)) from exc
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                update(runtime_attempts)
                .where(
                    runtime_attempts.c.id == attempt_id,
                    runtime_attempts.c.status == expected.value,
                    runtime_attempts.c.run_id
                    == select(runtime_runs.c.id)
                    .where(
                        runtime_runs.c.id == runtime_attempts.c.run_id,
                        runtime_runs.c.lease_owner == owner,
                        runtime_runs.c.lease_version == lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                    .scalar_subquery(),
                )
                .values(status=target.value, completed_at=now.isoformat())
                .returning(runtime_attempts)
            ).mappings().one_or_none()
            if row is None:
                raise RuntimeLeaseLost("attempt transition rejected by lease fence")
        return self._attempt_from_row(row)

    def _acquire_lease(
        self, run_id: str, *, owner: str, expected_status: RuntimeRunStatus
    ) -> RuntimeRun | None:
        now = datetime.now(UTC)
        target_status = (
            RuntimeRunStatus.RUNNING
            if expected_status
            in {RuntimeRunStatus.CREATED, RuntimeRunStatus.INTERRUPTED}
            else expected_status
        )
        if expected_status not in {
            RuntimeRunStatus.CREATED,
            RuntimeRunStatus.INTERRUPTED,
        }:
            return None
        if target_status != expected_status:
            ensure_run_transition(expected_status, target_status)
        statement = (
            update(runtime_runs)
            .where(
                runtime_runs.c.id == run_id,
                runtime_runs.c.status == expected_status.value,
                or_(
                    runtime_runs.c.lease_owner.is_(None),
                    runtime_runs.c.lease_expires_at.is_(None),
                    runtime_runs.c.lease_expires_at <= now.isoformat(),
                ),
            )
            .values(
                status=target_status.value,
                lease_owner=owner,
                lease_expires_at=(now + timedelta(seconds=self.lease_seconds)).isoformat(),
                lease_version=runtime_runs.c.lease_version + 1,
                started_at=func.coalesce(runtime_runs.c.started_at, now.isoformat()),
            )
            .returning(runtime_runs)
        )
        try:
            with self.engine.begin() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to acquire runtime lease") from exc
        return None if row is None else self._run_from_row(row)

    def renew_lease(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> RuntimeRun | None:
        now = datetime.now(UTC)
        statement = (
            update(runtime_runs)
            .where(
                runtime_runs.c.id == run_id,
                runtime_runs.c.status.in_(("running", "cancelling")),
                runtime_runs.c.lease_owner == owner,
                runtime_runs.c.lease_version == lease_version,
                runtime_runs.c.lease_expires_at > now.isoformat(),
            )
            .values(
                lease_expires_at=(now + timedelta(seconds=self.lease_seconds)).isoformat()
            )
            .returning(runtime_runs)
        )
        try:
            with self.engine.begin() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to renew runtime lease") from exc
        return None if row is None else self._run_from_row(row)

    def transition_run(
        self,
        run_id: str,
        *,
        expected: RuntimeRunStatus,
        target: RuntimeRunStatus,
        owner: str | None = None,
        lease_version: int | None = None,
    ) -> RuntimeRun:
        try:
            ensure_run_transition(expected, target)
        except ValueError as exc:
            raise RuntimeConflict(str(exc)) from exc
        if expected in {
            RuntimeRunStatus.RUNNING,
            RuntimeRunStatus.CANCELLING,
            RuntimeRunStatus.INTERRUPTED,
        } and (owner is None or lease_version is None):
            raise RuntimeLeaseLost("active runtime transition requires a lease fence")
        now = datetime.now(UTC)
        conditions = [
            runtime_runs.c.id == run_id,
            runtime_runs.c.status == expected.value,
        ]
        if owner is not None or lease_version is not None:
            if owner is None or lease_version is None:
                raise RuntimeLeaseLost("owner and lease_version are required together")
            conditions.extend(
                (
                    runtime_runs.c.lease_owner == owner,
                    runtime_runs.c.lease_version == lease_version,
                    runtime_runs.c.lease_expires_at > now.isoformat(),
                )
            )
        values: dict[str, object] = {"status": target.value}
        if target in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        }:
            values["completed_at"] = now.isoformat()
        if target in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
            RuntimeRunStatus.INTERRUPTED,
        }:
            values.update(lease_owner=None, lease_expires_at=None)
        with self.engine.begin() as connection:
            row = connection.execute(
                update(runtime_runs)
                .where(*conditions)
                .values(**values)
                .returning(runtime_runs)
            ).mappings().one_or_none()
            if row is None:
                exists = connection.execute(
                    select(runtime_runs.c.status).where(runtime_runs.c.id == run_id)
                ).scalar_one_or_none()
                if exists is None:
                    raise RuntimeNotFound(f"unknown runtime run: {run_id}")
                if owner is not None:
                    raise RuntimeLeaseLost("runtime lease fence rejected the write")
                raise RuntimeConflict(
                    f"runtime run status is {exists}, expected {expected.value}"
                )
            attempt_target = {
                RuntimeRunStatus.COMPLETED: RuntimeAttemptStatus.COMPLETED,
                RuntimeRunStatus.FAILED: RuntimeAttemptStatus.FAILED,
                RuntimeRunStatus.CANCELLED: RuntimeAttemptStatus.CANCELLED,
                RuntimeRunStatus.INTERRUPTED: RuntimeAttemptStatus.INTERRUPTED,
            }.get(target)
            if attempt_target is not None:
                connection.execute(
                    update(runtime_attempts)
                    .where(
                        runtime_attempts.c.run_id == run_id,
                        runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                    )
                    .values(
                        status=attempt_target.value,
                        completed_at=now.isoformat(),
                    )
                )
        return self._run_from_row(row)

    def request_cancel(self, run_id: str) -> RuntimeRun:
        now = datetime.now(UTC)
        source_run = self.get_run(run_id)
        owns_projection = (
            not source_run.is_v11
            or self.investigation_repository.get(
                source_run.investigation_id
            ).active_runtime_run_id
            == source_run.id
        )
        frozen_projection = (
            freeze_business_projection(
                self.investigation_repository,
                source_run.investigation_id,
                runtime_run_id=(source_run.id if source_run.is_v11 else None),
                authority_mode=(
                    source_run.authority_mode.value
                    if source_run.is_v11
                    else None
                ),
            )
            if source_run.status == RuntimeRunStatus.CREATED
            else None
        )
        with self.engine.begin() as connection:
            current = connection.execute(
                select(runtime_runs.c.status).where(runtime_runs.c.id == run_id)
            ).scalar_one_or_none()
            if current is None:
                raise RuntimeNotFound(f"unknown runtime run: {run_id}")
            if current == RuntimeRunStatus.CREATED.value:
                target = RuntimeRunStatus.CANCELLED
            elif current == RuntimeRunStatus.RUNNING.value:
                target = RuntimeRunStatus.CANCELLING
            elif current == RuntimeRunStatus.CANCELLING.value:
                return self.get_run(run_id)
            else:
                raise RuntimeConflict(f"run cannot be cancelled from {current}")
            values: dict[str, object] = {
                "status": target.value,
                "cancel_requested_at": now.isoformat(),
            }
            if target == RuntimeRunStatus.CANCELLED:
                values.update(
                    completed_at=now.isoformat(),
                    lease_owner=None,
                    lease_expires_at=None,
                    failure_category=RuntimeFailureCategory.CANCELLED.value,
                    frozen_business_projection=frozen_projection,
                )
            row = connection.execute(
                update(runtime_runs)
                .where(
                    runtime_runs.c.id == run_id,
                    runtime_runs.c.status == current,
                )
                .values(**values)
                .returning(runtime_runs)
            ).mappings().one()
            if target == RuntimeRunStatus.CANCELLED and owns_projection:
                record = self.investigation_repository.get(row["investigation_id"])
                self.investigation_repository.save_with_connection(
                    connection,
                    record.model_copy(
                        update={
                            "status": InvestigationStatus.CANCELLED,
                            "failure_reason": None,
                            "updated_at": now,
                        }
                    ),
                )
        return self._run_from_row(row)

    def _append_event_for_test(self, event: RuntimeEvent) -> RuntimeEvent:
        """仅供 Store 合约测试构造历史；生产事件必须经 RuntimeWriter。"""
        event = RuntimeEvent.model_validate(event.model_dump(mode="python"))
        try:
            with self.engine.begin() as connection:
                attempt_run_id = connection.execute(
                    select(runtime_attempts.c.run_id).where(
                        runtime_attempts.c.id == event.attempt_id
                    )
                ).scalar_one_or_none()
                if attempt_run_id != event.run_id:
                    raise RuntimeIntegrityError("event attempt does not belong to run")
                result = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == event.run_id,
                        runtime_runs.c.next_event_sequence == event.sequence - 1,
                    )
                    .values(next_event_sequence=event.sequence)
                )
                if result.rowcount != 1:
                    raise RuntimeConflict("runtime event sequence is not contiguous")
                connection.execute(
                    insert(runtime_events).values(**event.model_dump(mode="json"))
                )
        except (RuntimeIntegrityError, RuntimeConflict):
            raise
        except IntegrityError as exc:
            raise RuntimeConflict("runtime event conflicts with persisted sequence") from exc
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to append runtime event") from exc
        return event.model_copy(deep=True)

    def append_event_command(self, command) -> RuntimeEvent:
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                self.fault_injector.hit("unsafe_event_payload")
                run = connection.execute(
                    select(runtime_runs.c.id).where(
                        runtime_runs.c.id == command.run_id,
                        runtime_runs.c.status.in_(("running", "cancelling")),
                        runtime_runs.c.lease_owner == command.lease_owner,
                        runtime_runs.c.lease_version == command.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                ).scalar_one_or_none()
                if run is None:
                    raise RuntimeLeaseLost("runtime event rejected by run lease fence")
                attempt_run_id = connection.execute(
                    select(runtime_attempts.c.run_id).where(
                        runtime_attempts.c.id == command.attempt_id,
                        runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                    )
                ).scalar_one_or_none()
                if attempt_run_id != command.run_id:
                    raise RuntimeIntegrityError(
                        "event attempt is stale or belongs to another run"
                    )
                sequence = self._allocate_sequence(
                    connection,
                    command.run_id,
                    command.lease_owner,
                    command.lease_version,
                    now,
                )
                event = RuntimeEvent(
                    run_id=command.run_id,
                    attempt_id=command.attempt_id,
                    sequence=sequence,
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
                connection.execute(
                    insert(runtime_events).values(**event.model_dump(mode="json"))
                )
        except (
            ValidationError,
            RuntimeLeaseLost,
            RuntimeIntegrityError,
            RuntimeConflict,
            RuntimeInjectedFault,
        ):
            raise
        except Exception as exc:
            raise RuntimePersistenceError("runtime event transaction rolled back") from exc
        self._publish_events([event])
        return event

    def commit_terminal(self, commit: RuntimeTerminalCommit) -> RuntimeRun:
        """以单个 SQLite 事务提交 Run、Attempt 终态及对应事件。"""
        published: list[RuntimeEvent] = []
        source_run = self.get_run(commit.run_id)
        frozen_projection = freeze_business_projection(
            self.investigation_repository,
            source_run.investigation_id,
            runtime_run_id=(source_run.id if source_run.is_v11 else None),
            authority_mode=(
                source_run.authority_mode.value if source_run.is_v11 else None
            ),
        )
        from backend.runtime.diff import finalize_frozen_projection

        frozen_projection = finalize_frozen_projection(
            frozen_projection, self.list_checkpoints(source_run.id)
        )
        try:
            with self.engine.begin() as connection:
                # 冻结投影可能较慢；租约 fence 必须使用事务真正开始时的统一时刻。
                now = datetime.now(UTC)
                run_row = connection.execute(
                    select(runtime_runs).where(
                        runtime_runs.c.id == commit.run_id,
                        runtime_runs.c.status == commit.expected_run_status.value,
                        runtime_runs.c.lease_owner == commit.lease_owner,
                        runtime_runs.c.lease_version == commit.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                ).mappings().one_or_none()
                if run_row is None:
                    raise RuntimeLeaseLost("runtime terminal commit rejected by lease fence")
                attempt_row = connection.execute(
                    select(runtime_attempts).where(
                        runtime_attempts.c.id == commit.attempt_id,
                        runtime_attempts.c.run_id == commit.run_id,
                        runtime_attempts.c.status
                        == commit.expected_attempt_status.value,
                    )
                ).mappings().one_or_none()
                if attempt_row is None:
                    raise RuntimeIntegrityError("runtime terminal attempt conflict")
                ensure_run_transition(
                    commit.expected_run_status, commit.target_run_status
                )
                ensure_attempt_transition(
                    commit.expected_attempt_status, commit.target_attempt_status
                )
                for item in commit.events:
                    sequence = self._allocate_sequence(
                        connection,
                        commit.run_id,
                        commit.lease_owner,
                        commit.lease_version,
                        now,
                    )
                    event = RuntimeEvent(
                        run_id=commit.run_id,
                        attempt_id=commit.attempt_id,
                        sequence=sequence,
                        event_type=item.event_type,
                        phase=item.phase,
                        actor_type=item.actor_type,
                        safe_payload=item.safe_payload or {},
                    )
                    event = RuntimeEvent.model_validate(
                        event.model_dump(mode="python")
                    )
                    connection.execute(
                        insert(runtime_events).values(**event.model_dump(mode="json"))
                    )
                    published.append(event)
                self._hit_failpoint("persistence_mid_transaction")
                self.fault_injector.hit("persistence_mid_transaction")
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
                    investigation_id = run_row["investigation_id"]
                    call_rows = connection.execute(
                        select(tool_calls).where(
                            tool_calls.c.investigation_id == investigation_id
                        )
                    ).mappings()
                    for call_row in call_rows:
                        call = ToolCallRecord.model_validate(call_row["payload"])
                        if call.runtime_run_id != commit.run_id or call.status not in {
                            ToolCallStatus.PENDING,
                            ToolCallStatus.RUNNING,
                        }:
                            continue
                        interrupted_call = call.model_copy(
                            update={
                                "status": ToolCallStatus.INTERRUPTED,
                                "completed_at": now,
                                "error_message": terminal_message,
                            }
                        )
                        connection.execute(
                            update(tool_calls)
                            .where(tool_calls.c.id == call.id)
                            .values(
                                payload=interrupted_call.model_dump(mode="json")
                            )
                        )
                attempt_result = connection.execute(
                    update(runtime_attempts)
                    .where(
                        runtime_attempts.c.id == commit.attempt_id,
                        runtime_attempts.c.status
                        == commit.expected_attempt_status.value,
                    )
                    .values(
                        status=commit.target_attempt_status.value,
                        completed_at=now.isoformat(),
                        failure_category=(
                            commit.failure_category.value
                            if commit.failure_category is not None
                            else None
                        ),
                    )
                )
                if attempt_result.rowcount != 1:
                    raise RuntimeConflict("runtime terminal attempt changed concurrently")
                run_row = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == commit.run_id,
                        runtime_runs.c.status == commit.expected_run_status.value,
                        runtime_runs.c.lease_owner == commit.lease_owner,
                        runtime_runs.c.lease_version == commit.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                    .values(
                        status=commit.target_run_status.value,
                        failure_category=(
                            commit.failure_category.value
                            if commit.failure_category is not None
                            else None
                        ),
                        completed_at=now.isoformat(),
                        lease_owner=None,
                        lease_expires_at=None,
                        frozen_business_projection=frozen_projection,
                    )
                    .returning(runtime_runs)
                ).mappings().one_or_none()
                if run_row is None:
                    raise RuntimeLeaseLost("runtime terminal commit lost lease fence")
                investigation_status = {
                    RuntimeRunStatus.COMPLETED: InvestigationStatus.COMPLETED,
                    RuntimeRunStatus.FAILED: InvestigationStatus.FAILED,
                    RuntimeRunStatus.CANCELLED: InvestigationStatus.CANCELLED,
                }.get(commit.target_run_status)
                if investigation_status is not None:
                    record = self.investigation_repository.get(
                        run_row["investigation_id"]
                    )
                    if (
                        not source_run.is_v11
                        or record.active_runtime_run_id == commit.run_id
                    ):
                        self.investigation_repository.save_with_connection(
                            connection,
                            record.model_copy(
                                update={
                                    "status": investigation_status,
                                    "failure_reason": (
                                        safe_failure(commit.failure_category.value)
                                        if investigation_status
                                        == InvestigationStatus.FAILED
                                        and commit.failure_category is not None
                                        else None
                                    ),
                                    "updated_at": now,
                                }
                            ),
                        )
        except (
            ValidationError,
            RuntimeLeaseLost,
            RuntimeIntegrityError,
            RuntimeConflict,
            RuntimeInjectedFault,
        ):
            raise
        except Exception as exc:
            raise RuntimePersistenceError(
                "runtime terminal transaction rolled back"
            ) from exc
        self._publish_events(published)
        return self._run_from_row(run_row)

    def commit_tool(self, commit) -> ToolCallRecord:
        run_snapshot = self.get_run(commit.run_id)
        if run_snapshot.is_v11:
            record = self.investigation_repository.get(run_snapshot.investigation_id)
            if record.active_runtime_run_id != run_snapshot.id:
                raise RuntimeIntegrityError(
                    "V11 tool commit does not own active projection"
                )
            if getattr(commit.call, "runtime_run_id", None) != run_snapshot.id:
                raise RuntimeIntegrityError("V11 tool payload owner mismatch")
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                run_id = connection.execute(
                    select(runtime_runs.c.id).where(
                        runtime_runs.c.id == commit.run_id,
                        runtime_runs.c.status == RuntimeRunStatus.RUNNING.value,
                        runtime_runs.c.lease_owner == commit.lease_owner,
                        runtime_runs.c.lease_version == commit.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                ).scalar_one_or_none()
                if run_id is None:
                    raise RuntimeLeaseLost("tool commit rejected by run lease fence")
                if run_snapshot.is_v11:
                    record = self.investigation_repository.get_with_connection(
                        connection, run_snapshot.investigation_id
                    )
                    validate_v11_phase_ownership(run_snapshot, commit, record)
                attempt_run_id = connection.execute(
                    select(runtime_attempts.c.run_id).where(
                        runtime_attempts.c.id == commit.attempt_id,
                        runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                    )
                ).scalar_one_or_none()
                if attempt_run_id != commit.run_id:
                    raise RuntimeIntegrityError("tool commit attempt is stale")
                existing_row = connection.execute(
                    select(tool_calls.c.payload).where(
                        tool_calls.c.id == commit.call.id
                    )
                ).scalar_one_or_none()
                if existing_row is not None:
                    existing_call = ToolCallRecord.model_validate(existing_row)
                    if existing_call.status in {
                        ToolCallStatus.SUCCESS,
                        ToolCallStatus.FAILED,
                        ToolCallStatus.INTERRUPTED,
                    } and existing_call.status != commit.call.status:
                        raise RuntimeConflict(
                            "terminal tool call rejects a late result"
                        )
                existing_calls = [
                    ToolCallRecord.model_validate(payload)
                    for payload in connection.execute(
                        select(tool_calls.c.payload).where(
                            tool_calls.c.investigation_id
                            == run_snapshot.investigation_id
                        )
                    ).scalars()
                ]
                ensure_v11_tool_budget_available(
                    run_snapshot, existing_calls, commit.call
                )
                commit.business_mutation.apply_sqlite(
                    self.investigation_repository, connection
                )
                sequence = self._allocate_sequence(
                    connection,
                    commit.run_id,
                    commit.lease_owner,
                    commit.lease_version,
                    now,
                )
                event_type = {
                    ToolCallStatus.RUNNING: RuntimeEventType.TOOL_STARTED,
                    ToolCallStatus.SUCCESS: RuntimeEventType.TOOL_COMPLETED,
                }.get(commit.call.status, RuntimeEventType.TOOL_FAILED)
                event = RuntimeEvent(
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    sequence=sequence,
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
                connection.execute(
                    insert(runtime_events).values(**event.model_dump(mode="json"))
                )
        except (ValidationError, RuntimeLeaseLost, RuntimeIntegrityError, RuntimeConflict):
            raise
        except Exception as exc:
            raise RuntimePersistenceError("runtime tool transaction rolled back") from exc
        self._publish_events([event])
        return commit.call.model_copy(deep=True)

    def append_recovery_rejection(
        self,
        run_id: str,
        *,
        attempt_id: str,
        checkpoint_id: str | None,
        message: str | None = None,
    ) -> RuntimeEvent:
        try:
            with self.engine.begin() as connection:
                run_row = connection.execute(
                    select(
                        runtime_runs.c.investigation_id,
                        runtime_runs.c.execution_contract_version,
                        runtime_runs.c.authority_mode,
                    )
                    .select_from(
                        runtime_attempts.join(
                            runtime_runs,
                            runtime_attempts.c.run_id == runtime_runs.c.id,
                        )
                    )
                    .where(
                        runtime_attempts.c.id == attempt_id,
                        runtime_attempts.c.status
                        == RuntimeAttemptStatus.INTERRUPTED.value,
                        runtime_runs.c.id == run_id,
                        runtime_runs.c.status == RuntimeRunStatus.INTERRUPTED.value,
                    )
                ).mappings().one_or_none()
                if run_row is None:
                    raise RuntimeIntegrityError(
                        "recovery rejection attempt is not interrupted"
                    )
                frozen_projection = None
                if run_row["execution_contract_version"] == "v11":
                    frozen_projection = freeze_business_projection(
                        self.investigation_repository,
                        run_row["investigation_id"],
                        runtime_run_id=run_id,
                        authority_mode=run_row["authority_mode"],
                    )
                sequence = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == run_id,
                        runtime_runs.c.status == RuntimeRunStatus.INTERRUPTED.value,
                    )
                    .values(
                        next_event_sequence=runtime_runs.c.next_event_sequence + 1,
                        **(
                            {"frozen_business_projection": frozen_projection}
                            if frozen_projection is not None
                            else {}
                        ),
                    )
                    .returning(runtime_runs.c.next_event_sequence)
                ).scalar_one_or_none()
                if sequence is None:
                    raise RuntimeConflict(
                        "recovery rejection requires interrupted run"
                    )
                event = RuntimeEvent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    sequence=sequence,
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
                connection.execute(
                    insert(runtime_events).values(**event.model_dump(mode="json"))
                )
        except (RuntimeConflict, RuntimeIntegrityError):
            raise
        except Exception as exc:
            raise RuntimePersistenceError(
                "recovery rejection transaction rolled back"
            ) from exc
        self._publish_events([event])
        return event

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> list[RuntimeEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.engine.connect() as connection:
            if connection.execute(
                select(runtime_runs.c.id).where(runtime_runs.c.id == run_id)
            ).scalar_one_or_none() is None:
                raise RuntimeNotFound(f"unknown runtime run: {run_id}")
            rows = connection.execute(
                select(runtime_events)
                .where(
                    runtime_events.c.run_id == run_id,
                    runtime_events.c.sequence > after,
                )
                .order_by(runtime_events.c.sequence)
                .limit(limit)
            ).mappings().all()
        return [self._event_from_row(row) for row in rows]

    def get_checkpoint(self, checkpoint_id: str) -> RuntimeCheckpoint:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(runtime_checkpoints).where(runtime_checkpoints.c.id == checkpoint_id)
            ).mappings().one_or_none()
        if row is None:
            raise RuntimeNotFound(f"unknown runtime checkpoint: {checkpoint_id}")
        return self._checkpoint_from_row(row)

    def list_checkpoints(self, run_id: str) -> list[RuntimeCheckpoint]:
        with self.engine.connect() as connection:
            if connection.execute(
                select(runtime_runs.c.id).where(runtime_runs.c.id == run_id)
            ).scalar_one_or_none() is None:
                raise RuntimeNotFound(f"unknown runtime run: {run_id}")
            rows = connection.execute(
                select(runtime_checkpoints)
                .where(runtime_checkpoints.c.run_id == run_id)
                .order_by(runtime_checkpoints.c.event_sequence)
            ).mappings().all()
        return [self._checkpoint_from_row(row) for row in rows]

    def audit_expired_leases(self, now: datetime) -> list[RuntimeRun]:
        now_text = now.isoformat()
        published: list[RuntimeEvent] = []
        with self.engine.begin() as connection:
            ids = list(
                connection.execute(
                    select(runtime_runs.c.id).where(
                        runtime_runs.c.status.in_(("running", "cancelling")),
                        runtime_runs.c.lease_expires_at < now_text,
                    )
                ).scalars()
            )
            if not ids:
                return []
            attempt_rows = connection.execute(
                select(runtime_attempts.c.run_id, runtime_attempts.c.id).where(
                    runtime_attempts.c.run_id.in_(ids),
                    runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                )
            ).all()
            attempt_by_run = {run_id: attempt_id for run_id, attempt_id in attempt_rows}
            if set(attempt_by_run) != set(ids):
                raise RuntimeIntegrityError(
                    "expired runtime run has no active attempt"
                )
            rows = []
            for run_id in ids:
                snapshot = connection.execute(
                    select(runtime_runs).where(
                        runtime_runs.c.id == run_id
                    )
                ).mappings().one()
                frozen_projection = None
                if snapshot["execution_contract_version"] == "v11":
                    frozen_projection = freeze_business_projection(
                        self.investigation_repository,
                        snapshot["investigation_id"],
                        runtime_run_id=run_id,
                        authority_mode=snapshot["authority_mode"],
                    )
                previous_sequence = snapshot["next_event_sequence"]
                values = {
                    "status": RuntimeRunStatus.INTERRUPTED.value,
                    "failure_category": RuntimeFailureCategory.LEASE_LOST.value,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_event_sequence": previous_sequence + 2,
                }
                if frozen_projection is not None:
                    values["frozen_business_projection"] = frozen_projection
                row = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == run_id,
                        runtime_runs.c.status == snapshot["status"],
                        runtime_runs.c.lease_owner == snapshot["lease_owner"],
                        runtime_runs.c.lease_version == snapshot["lease_version"],
                        runtime_runs.c.lease_expires_at
                        == snapshot["lease_expires_at"],
                        runtime_runs.c.lease_expires_at < now_text,
                    )
                    .values(**values)
                    .returning(runtime_runs)
                ).mappings().one_or_none()
                if row is None:
                    continue
                investigation_id = snapshot["investigation_id"]
                call_rows = connection.execute(
                    select(tool_calls).where(
                        tool_calls.c.investigation_id == investigation_id
                    )
                ).mappings()
                for call_row in call_rows:
                    call = ToolCallRecord.model_validate(call_row["payload"])
                    if call.runtime_run_id != run_id or call.status not in {
                        ToolCallStatus.PENDING,
                        ToolCallStatus.RUNNING,
                    }:
                        continue
                    interrupted_call = call.model_copy(
                        update={
                            "status": ToolCallStatus.INTERRUPTED,
                            "completed_at": now,
                            "error_message": "runtime lease expired",
                        }
                    )
                    connection.execute(
                        update(tool_calls)
                        .where(tool_calls.c.id == call.id)
                        .values(payload=interrupted_call.model_dump(mode="json"))
                    )
                rows.append(row)
                attempt_id = attempt_by_run[run_id]
                connection.execute(
                    update(runtime_attempts)
                    .where(runtime_attempts.c.id == attempt_id)
                    .values(
                        status=RuntimeAttemptStatus.INTERRUPTED.value,
                        completed_at=now_text,
                        failure_category=RuntimeFailureCategory.LEASE_LOST.value,
                    )
                )
                run_event = RuntimeEvent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    sequence=previous_sequence + 1,
                    event_type=RuntimeEventType.RUN_INTERRUPTED,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "status": "interrupted",
                        "failure_category": "lease_lost",
                    },
                    occurred_at=now,
                )
                attempt_event = RuntimeEvent(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    sequence=previous_sequence + 2,
                    event_type=RuntimeEventType.ATTEMPT_INTERRUPTED,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "status": "interrupted",
                        "failure_category": "lease_lost",
                    },
                    occurred_at=now,
                )
                connection.execute(
                    insert(runtime_events),
                    [
                        run_event.model_dump(mode="json"),
                        attempt_event.model_dump(mode="json"),
                    ],
                )
                published.extend((run_event, attempt_event))
        self._publish_events(published)
        return [self._run_from_row(row) for row in rows]

    def reserve_model_turn(
        self, run_id: str, *, owner: str, lease_version: int
    ) -> int:
        """用带租约 CAS 的 SQLite UPDATE 原子扣减 V11 模型 turn 总预算。"""
        now = datetime.now(UTC).isoformat()
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == run_id,
                        runtime_runs.c.status.in_(
                            (
                                RuntimeRunStatus.RUNNING.value,
                                RuntimeRunStatus.CANCELLING.value,
                            )
                        ),
                        runtime_runs.c.lease_owner == owner,
                        runtime_runs.c.lease_version == lease_version,
                        runtime_runs.c.lease_expires_at > now,
                        runtime_runs.c.remaining_model_turns.is_not(None),
                        runtime_runs.c.remaining_model_turns > 0,
                    )
                    .values(
                        remaining_model_turns=runtime_runs.c.remaining_model_turns - 1
                    )
                    .returning(runtime_runs.c.remaining_model_turns)
                ).scalar_one_or_none()
                if result is not None:
                    return int(result)
                current = connection.execute(
                    select(
                        runtime_runs.c.status,
                        runtime_runs.c.lease_owner,
                        runtime_runs.c.lease_version,
                        runtime_runs.c.lease_expires_at,
                        runtime_runs.c.remaining_model_turns,
                        runtime_runs.c.execution_contract_version,
                    ).where(runtime_runs.c.id == run_id)
                ).mappings().one_or_none()
                if current is None:
                    raise RuntimeNotFound(f"unknown runtime run: {run_id}")
                if current["execution_contract_version"] != "v11":
                    raise RuntimeIntegrityError(
                        "model turn budget is only valid for V11"
                    )
                if current["remaining_model_turns"] is not None and int(
                    current["remaining_model_turns"]
                ) <= 0:
                    raise RuntimeConflict("V11 model turn budget exhausted")
                raise RuntimeLeaseLost("runtime lease fence rejected model turn reservation")
        except (RuntimeNotFound, RuntimeConflict, RuntimeLeaseLost, RuntimeIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise RuntimePersistenceError("failed to reserve V11 model turn") from exc

    def commit_phase(self, commit) -> RuntimeCheckpoint:
        with self._write_lock:
            return self._commit_phase(commit)

    def _commit_phase(self, commit) -> RuntimeCheckpoint:
        from backend.runtime.phases import (
            checkpoint_digest,
            durable_projection_digest,
            durable_token_usage,
            durable_tool_call_count,
            ensure_phase_precondition,
            phase_profile_for,
        )

        digest = checkpoint_digest(
            run_id=commit.run_id,
            attempt_id=commit.attempt_id,
            completed_phase=commit.phase,
            resume_state=commit.resume_state,
            schema_version=commit.schema_version,
        )
        run_snapshot = self.get_run(commit.run_id)
        record = self.investigation_repository.get(run_snapshot.investigation_id)
        previous_owner = record.active_runtime_run_id
        validate_v11_phase_ownership(
            run_snapshot,
            commit,
            record,
            previous_run=(
                self.get_run(previous_owner)
                if previous_owner is not None and previous_owner != run_snapshot.id
                else None
            ),
            previous_projection=(
                self.get_frozen_business_projection(previous_owner)
                if previous_owner is not None and previous_owner != run_snapshot.id
                else None
            ),
        )
        try:
            projection_digest = durable_projection_digest(
                repository=self.investigation_repository,
                investigation_id=run_snapshot.investigation_id,
                run_id=run_snapshot.id,
                resume_state=commit.resume_state,
                require_exact=True,
                mutation=commit.business_mutation,
            )
            consumed_tools = durable_tool_call_count(
                repository=self.investigation_repository,
                investigation_id=run_snapshot.investigation_id,
                run_id=run_snapshot.id,
                mutation=commit.business_mutation,
            )
            consumed_tokens = durable_token_usage(
                self.list_events(run_snapshot.id), run_snapshot.id
            )
        except ValueError as exc:
            raise RuntimeIntegrityError(str(exc)) from exc
        if (
            run_snapshot.tool_budget is not None
            and commit.resume_state.remaining_tool_budget
            != max(0, run_snapshot.tool_budget - consumed_tools)
        ):
            raise RuntimeIntegrityError(
                "checkpoint remaining tool budget differs from durable consumption"
            )
        if (
            run_snapshot.token_budget is not None
            and commit.resume_state.remaining_token_budget
            != max(0, run_snapshot.token_budget - consumed_tokens)
        ):
            raise RuntimeIntegrityError(
                "checkpoint remaining token budget differs from durable consumption"
            )
        if run_snapshot.is_v11 and (
            "execution_contract_digest" in run_snapshot.execution_contract
        ) and (
            commit.resume_state.remaining_model_turns
            != run_snapshot.remaining_model_turns
        ):
            raise RuntimeIntegrityError(
                "checkpoint remaining model turns differ from durable run budget"
            )
        try:
            with self.engine.begin() as connection:
                # 投影摘要等事务外计算可能跨越租约期限，进入事务后必须重新取时。
                now = datetime.now(UTC)
                run_row = connection.execute(
                    select(runtime_runs).where(
                        runtime_runs.c.id == commit.run_id,
                        runtime_runs.c.status.in_(("running", "cancelling")),
                        runtime_runs.c.lease_owner == commit.lease_owner,
                        runtime_runs.c.lease_version == commit.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                ).mappings().one_or_none()
                if run_row is None:
                    if connection.execute(
                        select(runtime_runs.c.id).where(runtime_runs.c.id == commit.run_id)
                    ).scalar_one_or_none() is None:
                        raise RuntimeNotFound(f"unknown runtime run: {commit.run_id}")
                    raise RuntimeLeaseLost("runtime lease fence rejected phase commit")
                existing = connection.execute(
                    select(runtime_checkpoints).where(
                        runtime_checkpoints.c.id == commit.checkpoint_id
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    checkpoint = self._checkpoint_from_row(existing)
                    if (
                        checkpoint.run_id == commit.run_id
                        and checkpoint.attempt_id == commit.attempt_id
                        and checkpoint.completed_phase == commit.phase
                        and checkpoint.state_digest == digest
                        and checkpoint.projection_digest == projection_digest
                    ):
                        return checkpoint
                    raise RuntimeConflict(
                        "checkpoint id was reused for another phase result"
                    )
                if run_row["investigation_id"] != commit.business_mutation.investigation_id:
                    raise RuntimeIntegrityError(
                        "phase business mutation crosses investigation"
                    )
                try:
                    ensure_phase_precondition(
                        current_phase=(
                            None
                            if run_row["current_phase"] is None
                            else RuntimePhase(run_row["current_phase"])
                        ),
                        latest_checkpoint_id=run_row["latest_checkpoint_id"],
                        commit=commit,
                        phase_order=phase_profile_for(
                            run_snapshot.execution_contract_version
                        ).order,
                    )
                except ValueError as exc:
                    raise RuntimeConflict(str(exc)) from exc
                attempt_run_id = connection.execute(
                    select(runtime_attempts.c.run_id).where(
                        runtime_attempts.c.id == commit.attempt_id,
                        runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                    )
                ).scalar_one_or_none()
                if attempt_run_id != commit.run_id:
                    raise RuntimeIntegrityError("phase attempt does not belong to active run")

                commit.business_mutation.apply_sqlite(
                    self.investigation_repository, connection
                )
                self.fault_injector.hit("persistence_mid_transaction")
                self._hit_failpoint("after_business_before_event")
                first_sequence = self._allocate_sequence(
                    connection,
                    commit.run_id,
                    commit.lease_owner,
                    commit.lease_version,
                    now,
                )
                phase_event = RuntimeEvent(
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    sequence=first_sequence,
                    event_type=(
                        RuntimeEventType.PHASE_SKIPPED
                        if commit.status == "skipped"
                        else RuntimeEventType.PHASE_COMPLETED
                    ),
                    phase=commit.phase,
                    actor_type=RuntimeActorType.PHASE,
                    evidence_ids=commit.resume_state.completed_evidence_ids,
                    safe_payload=commit.safe_payload,
                    schema_version=commit.schema_version,
                )
                second_sequence = self._allocate_sequence(
                    connection,
                    commit.run_id,
                    commit.lease_owner,
                    commit.lease_version,
                    now,
                )
                checkpoint_event = RuntimeEvent(
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    sequence=second_sequence,
                    event_type=RuntimeEventType.CHECKPOINT_CREATED,
                    phase=commit.phase,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "checkpoint_id": commit.checkpoint_id,
                        "event_sequence": second_sequence,
                        "state_digest": digest,
                        "projection_digest": projection_digest,
                    },
                    schema_version=commit.schema_version,
                )
                connection.execute(
                    insert(runtime_events),
                    [
                        phase_event.model_dump(mode="json"),
                        checkpoint_event.model_dump(mode="json"),
                    ],
                )
                checkpoint = RuntimeCheckpoint(
                    id=commit.checkpoint_id,
                    run_id=commit.run_id,
                    attempt_id=commit.attempt_id,
                    completed_phase=commit.phase,
                    event_sequence=second_sequence,
                    state_digest=digest,
                    projection_digest=projection_digest,
                    resume_state=commit.resume_state,
                    schema_version=commit.schema_version,
                )
                connection.execute(
                    insert(runtime_checkpoints).values(
                        **checkpoint.model_dump(mode="json")
                    )
                )
                advanced = connection.execute(
                    update(runtime_runs)
                    .where(
                        runtime_runs.c.id == commit.run_id,
                        runtime_runs.c.lease_owner == commit.lease_owner,
                        runtime_runs.c.lease_version == commit.lease_version,
                        runtime_runs.c.lease_expires_at > now.isoformat(),
                    )
                    .values(
                        current_phase=commit.phase.value,
                        latest_checkpoint_id=checkpoint.id,
                    )
                )
                if advanced.rowcount != 1:
                    raise RuntimeLeaseLost("runtime lease lost before phase advancement")
        except (RuntimeNotFound, RuntimeLeaseLost, RuntimeIntegrityError, RuntimeConflict):
            raise
        except Exception as exc:
            raise RuntimePersistenceError("sqlite phase commit rolled back") from exc

        self._publish_events([phase_event, checkpoint_event])
        return checkpoint

    def _allocate_sequence(
        self,
        connection,
        run_id: str,
        owner: str,
        lease_version: int,
        now: datetime,
    ) -> int:
        sequence = connection.execute(
            update(runtime_runs)
            .where(
                runtime_runs.c.id == run_id,
                runtime_runs.c.lease_owner == owner,
                runtime_runs.c.lease_version == lease_version,
                runtime_runs.c.lease_expires_at > now.isoformat(),
            )
            .values(next_event_sequence=runtime_runs.c.next_event_sequence + 1)
            .returning(runtime_runs.c.next_event_sequence)
        ).scalar_one_or_none()
        if sequence is None:
            raise RuntimeLeaseLost("runtime lease lost while allocating event sequence")
        return sequence

    def _hit_failpoint(self, point: str) -> None:
        if self._failpoint is not None:
            self._failpoint(point)

    def _publish_events(self, events: list[RuntimeEvent]) -> None:
        if self._event_publisher is None:
            return
        try:
            self._event_publisher([event.model_copy(deep=True) for event in events])
        except Exception as exc:
            # SSE/事件 Hub 是提交后的投影，发布失败不能回滚数据库事实。
            logger.warning("runtime event publication failed error_type=%s", type(exc).__name__)

    def _validate_linked_run(self, connection, run, linked_id, relation: str) -> None:
        if linked_id is None:
            return
        row = connection.execute(
            select(runtime_runs).where(runtime_runs.c.id == linked_id)
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeIntegrityError(f"unknown {relation} runtime run: {linked_id}")
        linked = self._run_from_row(row, connection)
        if linked.investigation_id != run.investigation_id:
            source_payload = connection.execute(
                select(investigations.c.event).where(
                    investigations.c.id == run.investigation_id
                )
            ).scalar_one_or_none()
            source_id = (
                source_payload.get("_diagops_source_investigation_id")
                if isinstance(source_payload, dict)
                else None
            )
            if not (
                relation == "parent"
                and run.is_v11
                and source_id == linked.investigation_id
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

    def _insert_attempt(
        self,
        connection,
        attempt: RuntimeAttempt,
        *,
        owner: str,
        lease_version: int,
    ) -> None:
        run_exists = connection.execute(
            select(runtime_runs.c.id).where(
                runtime_runs.c.id == attempt.run_id,
                runtime_runs.c.status == RuntimeRunStatus.RUNNING.value,
                runtime_runs.c.lease_owner == owner,
                runtime_runs.c.lease_version == lease_version,
                runtime_runs.c.lease_expires_at > datetime.now(UTC).isoformat(),
            )
        ).scalar_one_or_none()
        if run_exists is None:
            raise RuntimeLeaseLost("attempt creation rejected by lease fence")
        latest = connection.execute(
            select(func.max(runtime_attempts.c.attempt_number)).where(
                runtime_attempts.c.run_id == attempt.run_id
            )
        ).scalar_one()
        if attempt.attempt_number != ((latest or 0) + 1):
            raise RuntimeConflict("attempt_number must be consecutive per run")
        connection.execute(
            insert(runtime_attempts).values(
                **attempt.model_dump(mode="json"),
                trace_id=None,
                root_span_id=None,
            )
        )

    def _run_from_row(self, row, connection=None) -> RuntimeRun:
        data = dict(row)
        next_event_sequence = int(data.pop("next_event_sequence", 0) or 0)
        data.pop("frozen_business_projection", None)
        data.pop("benchmark_replay_locator", None)
        try:
            return RuntimeRun.model_validate(data)
        except ValidationError:
            if not self._has_contract_projection_mismatch(data):
                raise
            now = datetime.now(UTC)
            values = {
                "status": RuntimeRunStatus.FAILED.value,
                "failure_category": RuntimeFailureCategory.CONTRACT_INTEGRITY.value,
                "completed_at": data.get("completed_at") or now.isoformat(),
                "lease_owner": None,
                "lease_expires_at": None,
            }
            if connection is None:
                with self.engine.begin() as write_connection:
                    self._repair_contract_integrity(
                        write_connection,
                        data,
                        values,
                        next_event_sequence,
                    )
            else:
                self._repair_contract_integrity(
                    connection,
                    data,
                    values,
                    next_event_sequence,
                )
            data.update(values)
            data.pop("next_event_sequence", None)
            data.pop("frozen_business_projection", None)
            data["execution_contract"] = self._safe_contract_projection(data)
            return RuntimeRun.model_validate(data)

    def _repair_contract_integrity(
        self,
        connection,
        data: dict,
        values: dict,
        next_event_sequence: int,
    ) -> None:
        """在同一事务内终止活动 Attempt、写失败事件并同步 V11 业务投影。"""
        now = datetime.now(UTC)
        attempt = connection.execute(
            select(runtime_attempts.c.id).where(
                runtime_attempts.c.run_id == data["id"],
                runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
            )
        ).scalar_one_or_none()
        if attempt is not None:
            connection.execute(
                update(runtime_attempts)
                .where(
                    runtime_attempts.c.id == attempt,
                    runtime_attempts.c.status == RuntimeAttemptStatus.RUNNING.value,
                )
                .values(
                    status=RuntimeAttemptStatus.FAILED.value,
                    completed_at=now.isoformat(),
                    failure_category=RuntimeFailureCategory.CONTRACT_INTEGRITY.value,
                )
            )
            next_event_sequence += 1
            event = RuntimeEvent(
                run_id=data["id"],
                attempt_id=attempt,
                sequence=next_event_sequence,
                event_type=RuntimeEventType.RUN_FAILED,
                actor_type=RuntimeActorType.RUNTIME,
                safe_payload={
                    "status": "failed",
                    "failure_category": RuntimeFailureCategory.CONTRACT_INTEGRITY.value,
                },
                occurred_at=now,
            )
            connection.execute(insert(runtime_events).values(**event.model_dump(mode="json")))
            values["next_event_sequence"] = next_event_sequence
        if data.get("execution_contract_version") == "v11":
            try:
                values["frozen_business_projection"] = freeze_business_projection(
                    self.investigation_repository,
                    data["investigation_id"],
                    runtime_run_id=data["id"],
                    authority_mode=data.get("authority_mode", "agent"),
                )
            except (AttributeError, TypeError, ValueError):
                values["frozen_business_projection"] = None
        record = self.investigation_repository.get_with_connection(
            connection, data["investigation_id"]
        )
        if not data.get("execution_contract_version") == "v11" or record.active_runtime_run_id in {
            None,
            data["id"],
        }:
            self.investigation_repository.save_with_connection(
                connection,
                record.model_copy(
                    update={
                        "status": InvestigationStatus.FAILED,
                        "failure_reason": safe_failure(
                            RuntimeFailureCategory.CONTRACT_INTEGRITY.value
                        ),
                        "updated_at": now,
                    }
                ),
            )
        # 修复必须随同事务持久化校正后的契约投影；否则每次读取都会重复修复，
        # 导致 V11 重复 freeze/reseal 且业务投影被读路径反复改写。
        values["execution_contract"] = self._safe_contract_projection(data)
        connection.execute(
            update(runtime_runs)
            .where(runtime_runs.c.id == data["id"])
            .values(**values)
        )

    @staticmethod
    def _has_contract_projection_mismatch(data: dict) -> bool:
        contract = data.get("execution_contract")
        if not isinstance(contract, dict):
            return True
        for field_name in (
            "execution_contract_version",
            "authority_mode",
            "model_provider",
            "model_name",
            "prompt_version",
            "tool_budget",
            "token_budget",
            "timeout_seconds",
        ):
            if field_name in contract:
                expected = data.get(field_name)
                if isinstance(expected, str):
                    expected = expected
                if contract[field_name] != expected:
                    return True
        version = data.get("execution_contract_version", "v10_legacy")
        if version == "v11" and not {
            "model_provider",
            "model_name",
            "prompt_version",
            "tool_budget",
            "token_budget",
            "timeout_seconds",
        } <= contract.keys():
            return True
        if version == "v11" and "execution_contract_digest" in contract:
            try:
                validate_v11_execution_contract(contract)
            except ValueError:
                return True
        return False

    @staticmethod
    def _safe_contract_projection(data: dict) -> dict:
        version = data.get("execution_contract_version", "v10_legacy")
        authority = data.get("authority_mode", "legacy_deterministic")
        if version not in {"v10_legacy", "v11"}:
            version, authority = "v10_legacy", "legacy_deterministic"
        if version == "v11" and authority != "agent":
            version, authority = "v10_legacy", "legacy_deterministic"
        return {
            "execution_contract_version": version,
            "authority_mode": authority,
            "run_kind": data.get("run_kind"),
            "run_reason": data.get("run_reason"),
            "strategy": data.get("strategy"),
            "model_provider": data.get("model_provider"),
            "model_name": data.get("model_name"),
            "prompt_version": data.get("prompt_version"),
            "tool_budget": data.get("tool_budget"),
            "token_budget": data.get("token_budget"),
            "timeout_seconds": data.get("timeout_seconds", 60.0),
        }

    @staticmethod
    def _event_from_row(row) -> RuntimeEvent:
        return RuntimeEvent.model_validate(dict(row))

    @staticmethod
    def _attempt_from_row(row) -> RuntimeAttempt:
        data = dict(row)
        data.pop("trace_id", None)
        data.pop("root_span_id", None)
        return RuntimeAttempt.model_validate(data)

    @staticmethod
    def _checkpoint_from_row(row) -> RuntimeCheckpoint:
        return RuntimeCheckpoint.model_validate(dict(row))
