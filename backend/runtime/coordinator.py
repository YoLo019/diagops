from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import suppress
from datetime import UTC, datetime

from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallStatus
from backend.runtime.faults import NoFaultInjector, RuntimeInjectedFault
from backend.runtime.phases import (
    RUNTIME_PHASE_ORDER,
    BusinessMutation,
    PhaseCommit,
    PhaseInput,
    ToolCommit,
    checkpoint_digest,
    durable_projection_digest,
    durable_token_usage,
    durable_tool_call_count,
)
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeLeaseLost,
    RuntimePersistenceError,
    RuntimeStore,
    RuntimeTerminalCommit,
    RuntimeTerminalEvent,
)
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeEventCommand, RuntimeWriter
from backend.tools.registry import ToolInvocationResult

logger = logging.getLogger(__name__)


class _RuntimeCancellationRequested(Exception):
    """表示持有有效 lease 的 Run 已请求取消，应在安全边界收敛终态。"""


class RuntimeCoordinator:
    """管理 Run/Attempt 生命周期；RCA 决策仍由注入的 PhaseExecutor 负责。"""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        writer: RuntimeWriter,
        phase_executor,
        heartbeat_seconds: int = 10,
        fault_injector=None,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if heartbeat_seconds < 1:
            raise ValueError("heartbeat_seconds must be positive")
        self.store = store
        self.writer = writer
        self.phase_executor = phase_executor
        self.heartbeat_seconds = heartbeat_seconds
        self.fault_injector = fault_injector or NoFaultInjector()
        self.telemetry = telemetry or RuntimeTelemetry.disabled()
        self.store.fault_injector = self.fault_injector
        self.writer.fault_injector = self.fault_injector
        self.phase_executor.fault_injector = self.fault_injector
        self.phase_executor.telemetry = self.telemetry
        self._local_cancel: dict[str, asyncio.Event] = {}
        self._active: dict[str, tuple[str, int]] = {}
        self._phase_spans: dict[str, object] = {}
        self._agent_spans: dict[tuple[str, str], object] = {}
        self._model_spans: dict[tuple[str, str], object] = {}
        self._tool_spans: dict[tuple[str, str], object] = {}
        self._state_lock = asyncio.Lock()

    async def execute(self, run_id: str, owner: str) -> RuntimeRun:
        await self.writer.start()
        attempt_number = len(self.store.list_attempts(run_id)) + 1
        attempt = RuntimeAttempt(
            run_id=run_id,
            attempt_number=attempt_number,
            status=RuntimeAttemptStatus.RUNNING,
        )
        run, attempt = self.store.acquire_lease_and_create_attempt(
            run_id,
            attempt=attempt,
            owner=owner,
            expected_status=RuntimeRunStatus.CREATED,
        )
        return await self._execute_owned(run, attempt, owner)

    async def resume(self, run_id: str, owner: str) -> RuntimeRun:
        await self.writer.start()
        run = self.store.get_run(run_id)
        if run.status != RuntimeRunStatus.INTERRUPTED:
            raise RuntimeConflict("only interrupted runtime runs can resume")
        checkpoint = None
        if run.latest_checkpoint_id is not None:
            try:
                checkpoint = self._validated_checkpoint(run)
            except RuntimeConflict:
                attempts = self.store.list_attempts(run_id)
                if attempts:
                    self.store.append_recovery_rejection(
                        run_id,
                        attempt_id=attempts[-1].id,
                        checkpoint_id=run.latest_checkpoint_id,
                    )
                raise
        elif run.current_phase is not None or self.store.list_checkpoints(run.id):
            raise RuntimeConflict("runtime position lacks its latest checkpoint")
        recovery_state = self._effective_resume_state(run, checkpoint)
        attempt = RuntimeAttempt(
            run_id=run_id,
            attempt_number=len(self.store.list_attempts(run_id)) + 1,
            resume_from_checkpoint_id=checkpoint.id if checkpoint is not None else None,
            status=RuntimeAttemptStatus.RUNNING,
        )
        try:
            self.fault_injector.hit("concurrent_resume")
            leased, attempt = self.store.acquire_lease_and_create_attempt(
                run_id,
                attempt=attempt,
                owner=owner,
                expected_status=RuntimeRunStatus.INTERRUPTED,
            )
        except RuntimeConflict as exc:
            raise RuntimeConflict("runtime resume conflict") from exc
        return await self._execute_owned(
            leased,
            attempt,
            owner,
            recovery_checkpoint_id=checkpoint.id if checkpoint is not None else None,
            recovery_state=recovery_state,
            recovery=True,
        )

    async def request_cancel(self, run_id: str) -> RuntimeRun:
        async with self._state_lock:
            first_request = self.store.get_run(run_id).cancel_requested_at is None
            run = self.store.request_cancel(run_id)
            local = self._local_cancel.get(run_id)
            if local is not None:
                local.set()
            active = self._active.get(run_id)
            if (
                first_request
                and active is not None
                and run.status == RuntimeRunStatus.CANCELLING
            ):
                self.fault_injector.hit("cancel_parallel_specialists")
                owner, lease_version = active
                with suppress(RuntimeConflict, RuntimeLeaseLost):
                    await self.writer.submit_event(
                        RuntimeEventCommand(
                            run_id=run_id,
                            attempt_id=self._running_attempt(run_id).id,
                            lease_owner=owner,
                            lease_version=lease_version,
                            event_type=RuntimeEventType.RUN_CANCEL_REQUESTED,
                            actor_type=RuntimeActorType.RUNTIME,
                            safe_payload={"status": RuntimeRunStatus.CANCELLING.value},
                        )
                    )
        return run

    def audit_expired_leases(self, now: datetime | None = None) -> list[RuntimeRun]:
        """只标记 interrupted，不启动任何 Agent、Provider 或 Tool。"""
        return self.store.audit_expired_leases(now or datetime.now(UTC))

    async def shutdown(self) -> None:
        await self.writer.shutdown()

    def check_execution(self, run_id: str, owner: str, lease_version: int) -> None:
        self.fault_injector.hit("lease_lost")
        run = self.store.get_run(run_id)
        if (
            run.status not in {RuntimeRunStatus.RUNNING, RuntimeRunStatus.CANCELLING}
            or run.lease_owner != owner
            or run.lease_version != lease_version
            or run.lease_expires_at is None
            or run.lease_expires_at <= datetime.now(UTC)
        ):
            raise RuntimeLeaseLost("runtime execution fence rejected late result")
        local_cancel = self._local_cancel.get(run_id)
        if run.status == RuntimeRunStatus.CANCELLING or (
            local_cancel is not None and local_cancel.is_set()
        ):
            raise _RuntimeCancellationRequested

    async def _execute_owned(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        recovery_checkpoint_id: str | None = None,
        recovery_state: RuntimeResumeState | None = None,
        recovery: bool = False,
    ) -> RuntimeRun:
        cancel_event = asyncio.Event()
        async with self._state_lock:
            if run.id in self._active:
                raise RuntimeConflict("runtime run is already active locally")
            self._active[run.id] = (owner, run.lease_version)
            self._local_cancel[run.id] = cancel_event
        heartbeat = asyncio.create_task(
            self._renew_loop(run.id, owner, run.lease_version, cancel_event)
        )
        active_phase = None
        active_phase_scope = None
        previous_reference = None
        prior_attempts = [
            item
            for item in self.store.list_attempts(run.id)
            if item.attempt_number < attempt.attempt_number
        ]
        if prior_attempts:
            previous_reference = self.store.get_attempt_trace_context(prior_attempts[-1].id)
        attempt_scope = self.telemetry.attempt(
            run,
            attempt,
            previous=previous_reference,
        )
        attempt_trace = attempt_scope.__enter__()
        if attempt_trace.reference is not None:
            try:
                self.store.set_attempt_trace_context(
                    attempt.id,
                    attempt_trace.reference,
                )
            except Exception:
                # Trace context 是审计增强字段，写入失败不得改变 Run 生命周期。
                logger.warning(
                    "attempt trace context persistence failed category=otel_context_persistence"
                )
        try:
            await self._emit_start(run, attempt, owner)
            if recovery:
                await self._emit_recovery_events(
                    run,
                    attempt,
                    owner,
                    recovery_checkpoint_id,
                )
            resume_state, start_index = self._resume_position(run)
            if recovery_state is not None:
                resume_state = recovery_state
            available_tool_budget = (
                resume_state.remaining_tool_budget
                if recovery and run.tool_budget is not None
                else run.tool_budget
            )
            available_token_budget = (
                resume_state.remaining_token_budget
                if recovery and run.token_budget is not None
                else run.token_budget
            )
            for phase in RUNTIME_PHASE_ORDER[start_index:]:
                active_phase = phase
                active_phase_scope = self.telemetry.span(
                    "runtime.phase",
                    {
                        "investigation_id": run.investigation_id,
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "phase": phase.value,
                        "status": "running",
                    },
                )
                phase_span = active_phase_scope.__enter__()
                self._phase_spans[attempt.id] = phase_span
                await self._cancel_if_requested(run.id, attempt, owner, run.lease_version)
                self.fault_injector.hit("before_phase_start")
                await self.writer.submit_event(
                    RuntimeEventCommand(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        lease_owner=owner,
                        lease_version=run.lease_version,
                        event_type=RuntimeEventType.PHASE_STARTED,
                        actor_type=RuntimeActorType.PHASE,
                        phase=phase,
                        safe_payload={"status": "running"},
                    )
                )
                self.check_execution(run.id, owner, run.lease_version)
                output = await self.phase_executor.execute_phase(
                    PhaseInput(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        phase=phase,
                        resume_state=resume_state,
                        investigation_id=run.investigation_id,
                        strategy=run.strategy,
                        run_reason=run.run_reason,
                        model_provider=run.model_provider,
                        model_name=run.model_name,
                        prompt_version=run.prompt_version,
                        tool_budget=available_tool_budget,
                        token_budget=available_token_budget,
                        timeout_seconds=run.timeout_seconds,
                        check_execution=lambda: self.check_execution(
                            run.id, owner, run.lease_version
                        ),
                        resolve_tool_result=lambda key: self._resolve_tool_result(run, key),
                        persist_tool_start=lambda call, phase=phase: self._persist_tool_start(
                            run, attempt, owner, phase, call
                        ),
                        persist_tool_result=lambda result, phase=phase: self._persist_tool_result(
                            run, attempt, owner, phase, result
                        ),
                        persist_agent_event=lambda actor_name, status, phase=phase: (
                            self._persist_agent_event(
                                run, attempt, owner, phase, actor_name, status
                            )
                        ),
                        persist_model_event=lambda execution_id,
                        status,
                        input_tokens=0,
                        output_tokens=0,
                        actor_name="CoordinatorAgent",
                        phase=phase: (
                            self._persist_model_event(
                                run,
                                attempt,
                                owner,
                                phase,
                                execution_id,
                                status,
                                input_tokens,
                                output_tokens,
                                actor_name,
                            )
                        ),
                        hit_fault=self.fault_injector.hit,
                    )
                )
                self.check_execution(run.id, owner, run.lease_version)
                current = self.store.get_run(run.id)
                checkpoint = await self.writer.submit(
                    PhaseCommit(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        lease_owner=owner,
                        lease_version=run.lease_version,
                        phase=phase,
                        business_mutation=output.business_mutation,
                        safe_payload=output.safe_payload,
                        resume_state=output.resume_state,
                        status=output.status,
                        expected_checkpoint_id=current.latest_checkpoint_id,
                        expected_previous_phase=current.current_phase,
                    )
                )
                resume_state = checkpoint.resume_state
                available_tool_budget = (
                    resume_state.remaining_tool_budget if run.tool_budget is not None else None
                )
                available_token_budget = (
                    resume_state.remaining_token_budget if run.token_budget is not None else None
                )
                active_phase_scope.__exit__(None, None, None)
                self._phase_spans.pop(attempt.id, None)
                active_phase_scope = None
                active_phase = None
            await self._complete(run, attempt, owner)
            return self.store.get_run(run.id)
        except _RuntimeCancellationRequested:
            await self._finish_cancel(run.id, attempt, owner, run.lease_version)
            return self.store.get_run(run.id)
        except asyncio.CancelledError:
            await self._finish_cancel(run.id, attempt, owner, run.lease_version)
            raise
        except RuntimeLeaseLost:
            # lease loss 后不能用旧 fence 写状态；由恢复审计在过期后标记 interrupted。
            return self.store.get_run(run.id)
        except RuntimeInjectedFault:
            # Fault injector 模拟进程在边界崩溃；保留 running 供 lease audit 收敛 interrupted。
            return self.store.get_run(run.id)
        except RuntimePersistenceError as exc:
            # 持久化层需要用稳定错误包装事务异常；仅当根因是故障注入时，仍按进程崩溃语义处理。
            if isinstance(exc.__cause__, RuntimeInjectedFault):
                return self.store.get_run(run.id)
            raise
        except Exception:
            self.fault_injector.hit("parallel_session_failure")
            await self._fail_if_owned(
                run.id,
                attempt,
                owner,
                run.lease_version,
                phase=active_phase,
            )
            raise
        finally:
            exception = sys.exc_info()
            if active_phase_scope is not None:
                active_phase_scope.__exit__(*exception)
            self._phase_spans.pop(attempt.id, None)
            self._end_attempt_spans(attempt.id, failed=exception[0] is not None)
            attempt_scope.__exit__(*exception)
            cancel_event.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            release_attempt = getattr(self.phase_executor, "release_attempt", None)
            if release_attempt is not None:
                release_attempt(run.id, attempt.id)
            async with self._state_lock:
                self._active.pop(run.id, None)
                self._local_cancel.pop(run.id, None)

    async def _emit_start(self, run: RuntimeRun, attempt: RuntimeAttempt, owner: str) -> None:
        for event_type, actor in (
            (RuntimeEventType.RUN_STARTED, RuntimeActorType.RUNTIME),
            (RuntimeEventType.ATTEMPT_STARTED, RuntimeActorType.RUNTIME),
        ):
            await self.writer.submit_event(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    event_type=event_type,
                    actor_type=actor,
                    safe_payload={"status": "running"},
                )
            )

    def _resolve_tool_result(self, run: RuntimeRun, key: str):
        repository = getattr(self.store, "investigation_repository", None)
        if repository is None:
            return None
        return next(
            (
                call
                for call in repository.list_tool_calls(run.investigation_id)
                if call.runtime_run_id == run.id
                and call.idempotency_key == key
                and call.status == ToolCallStatus.SUCCESS
            ),
            None,
        )

    async def _persist_tool_start(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
        call,
    ):
        agent_name = getattr(call.agent_name, "value", str(call.agent_name))
        span = self.telemetry.start_span(
            "runtime.tool",
            {
                "investigation_id": run.investigation_id,
                "run_id": run.id,
                "attempt_id": attempt.id,
                "tool_name": call.tool_name,
                "status": call.status.value,
            },
            parent=self._agent_spans.get((attempt.id, agent_name)),
        )
        self._tool_spans[(attempt.id, call.id)] = span
        try:
            return await self.writer.submit_tool(
                ToolCommit(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    business_mutation=BusinessMutation(
                        investigation_id=run.investigation_id,
                        tool_calls=(call,),
                    ),
                    call=call,
                    phase=phase,
                )
            )
        except Exception:
            self._finish_span(span, "failed")
            self._tool_spans.pop((attempt.id, call.id), None)
            raise

    async def _persist_tool_result(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
        result: ToolInvocationResult,
    ):
        repository = self.store.investigation_repository
        record = repository.get(run.investigation_id)
        evidence = {item.id: item for item in record.evidence}
        evidence.update({item.id: item for item in result.evidence})
        provider_results = {item.id: item for item in record.provider_results}
        provider_results.update({item.id: item for item in result.provider_results})
        record = record.model_copy(
            update={
                "evidence": list(evidence.values()),
                "provider_results": list(provider_results.values()),
                "updated_at": datetime.now(UTC),
            }
        )
        span = self._tool_spans.pop((attempt.id, result.call.id), None)
        try:
            persisted = await self.writer.submit_tool(
                ToolCommit(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    business_mutation=BusinessMutation(
                        investigation_id=run.investigation_id,
                        investigation=record,
                        tool_calls=(result.call,),
                    ),
                    call=result.call,
                    phase=phase,
                )
            )
        except Exception:
            self._finish_span(span, "failed")
            raise
        self._finish_span(
            span,
            result.call.status.value,
            evidence_count=len(result.evidence),
        )
        self.fault_injector.hit("tool_after_commit_before_checkpoint")
        return persisted

    async def _persist_model_event(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
        execution_id: str,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        actor_name: str = "CoordinatorAgent",
    ) -> None:
        event_type = {
            "started": RuntimeEventType.MODEL_STARTED,
            "completed": RuntimeEventType.MODEL_COMPLETED,
            "failed": RuntimeEventType.MODEL_FAILED,
        }[status]
        key = (attempt.id, execution_id)
        if status == "started":
            self._model_spans[key] = self.telemetry.start_span(
                "runtime.model",
                {
                    "investigation_id": run.investigation_id,
                    "run_id": run.id,
                    "attempt_id": attempt.id,
                    "provider": run.model_provider,
                    "model": run.model_name,
                    "status": status,
                },
                parent=self._agent_spans.get((attempt.id, actor_name)),
            )
        try:
            await self.writer.submit_event(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    event_type=event_type,
                    actor_type=RuntimeActorType.MODEL,
                    phase=phase,
                    actor_name=actor_name,
                    execution_id=execution_id,
                    safe_payload={
                        "status": status,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                )
            )
        except Exception:
            if status == "started":
                self._finish_span(self._model_spans.pop(key, None), "failed")
            raise
        if status != "started":
            self._finish_span(
                self._model_spans.pop(key, None),
                status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    async def _persist_agent_event(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
        actor_name: str,
        status: str,
    ) -> None:
        event_type = {
            "started": RuntimeEventType.AGENT_STARTED,
            "completed": RuntimeEventType.AGENT_COMPLETED,
            "failed": RuntimeEventType.AGENT_FAILED,
        }[status]
        key = (attempt.id, actor_name)
        if status == "started":
            self._agent_spans[key] = self.telemetry.start_span(
                "runtime.agent",
                {
                    "investigation_id": run.investigation_id,
                    "run_id": run.id,
                    "attempt_id": attempt.id,
                    "phase": phase.value,
                    "agent_name": actor_name,
                    "status": status,
                },
                parent=self._phase_spans.get(attempt.id),
            )
        try:
            await self.writer.submit_event(
                RuntimeEventCommand(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner=owner,
                lease_version=run.lease_version,
                event_type=event_type,
                actor_type=RuntimeActorType.AGENT,
                phase=phase,
                actor_name=actor_name,
                safe_payload={"status": "running" if status == "started" else status},
            )
            )
        except Exception:
            if status == "started":
                self._finish_span(self._agent_spans.pop(key, None), "failed")
            raise
        if status != "started":
            self._finish_span(self._agent_spans.pop(key, None), status)

    @staticmethod
    def _finish_span(span, status: str, **attributes) -> None:
        if span is None:
            return
        span.set_attribute("status", status)
        for key, value in RuntimeTelemetry.safe_attributes(attributes).items():
            span.set_attribute(key, value)
        span.end()

    def _end_attempt_spans(self, attempt_id: str, *, failed: bool) -> None:
        status = "failed" if failed else "interrupted"
        for spans in (self._tool_spans, self._model_spans, self._agent_spans):
            for key, span in list(spans.items()):
                if key[0] != attempt_id:
                    continue
                spans.pop(key, None)
                self._finish_span(span, status)

    async def _emit_recovery_events(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        checkpoint_id: str | None,
    ) -> None:
        for event_type, status in (
            (RuntimeEventType.RECOVERY_STARTED, "running"),
            (RuntimeEventType.RECOVERY_COMPLETED, "completed"),
        ):
            await self.writer.submit_event(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    event_type=event_type,
                    actor_type=RuntimeActorType.RECOVERY,
                    safe_payload={
                        "status": status,
                        "resume_attempt_number": attempt.attempt_number,
                        **({"checkpoint_id": checkpoint_id} if checkpoint_id is not None else {}),
                    },
                )
            )

    async def _complete(self, run: RuntimeRun, attempt: RuntimeAttempt, owner: str) -> None:
        await self._cancel_if_requested(run.id, attempt, owner, run.lease_version)
        commit = RuntimeTerminalCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner=owner,
                lease_version=run.lease_version,
                expected_run_status=RuntimeRunStatus.RUNNING,
                target_run_status=RuntimeRunStatus.COMPLETED,
                expected_attempt_status=RuntimeAttemptStatus.RUNNING,
                target_attempt_status=RuntimeAttemptStatus.COMPLETED,
                events=tuple(
                    RuntimeTerminalEvent(
                        event_type=event_type,
                        actor_type=RuntimeActorType.RUNTIME,
                        safe_payload={"status": "completed"},
                    )
                    for event_type in (
                        RuntimeEventType.ATTEMPT_COMPLETED,
                        RuntimeEventType.RUN_COMPLETED,
                    )
                ),
            )
        try:
            await self.writer.submit_terminal(commit)
        except (RuntimeConflict, RuntimeLeaseLost):
            if self.store.get_run(run.id).status == RuntimeRunStatus.CANCELLING:
                raise _RuntimeCancellationRequested from None
            raise

    async def _cancel_if_requested(
        self,
        run_id: str,
        attempt: RuntimeAttempt,
        owner: str,
        lease_version: int,
    ) -> None:
        run = self.store.get_run(run_id)
        if run.status == RuntimeRunStatus.CANCELLING or self._local_cancel[run_id].is_set():
            raise _RuntimeCancellationRequested

    async def _finish_cancel(
        self,
        run_id: str,
        attempt: RuntimeAttempt,
        owner: str,
        lease_version: int,
    ) -> None:
        run = self.store.get_run(run_id)
        if run.status != RuntimeRunStatus.CANCELLING:
            return
        with suppress(RuntimeConflict, RuntimeLeaseLost):
            await self.writer.submit_terminal(
                RuntimeTerminalCommit(
                    run_id=run_id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=lease_version,
                    expected_run_status=RuntimeRunStatus.CANCELLING,
                    target_run_status=RuntimeRunStatus.CANCELLED,
                    expected_attempt_status=RuntimeAttemptStatus.RUNNING,
                    target_attempt_status=RuntimeAttemptStatus.CANCELLED,
                    events=(
                        RuntimeTerminalEvent(
                            event_type=RuntimeEventType.RUN_CANCELLED,
                            actor_type=RuntimeActorType.RUNTIME,
                            safe_payload={"status": "cancelled"},
                        ),
                    ),
                    failure_category=RuntimeFailureCategory.CANCELLED,
                )
            )

    async def _fail_if_owned(
        self,
        run_id: str,
        attempt: RuntimeAttempt,
        owner: str,
        lease_version: int,
        *,
        phase: RuntimePhase | None = None,
    ) -> None:
        run = self.store.get_run(run_id)
        if run.status != RuntimeRunStatus.RUNNING:
            return
        with suppress(RuntimeConflict, RuntimeLeaseLost):
            events: list[RuntimeTerminalEvent] = []
            if phase is not None:
                events.append(
                    RuntimeTerminalEvent(
                        event_type=RuntimeEventType.PHASE_FAILED,
                        actor_type=RuntimeActorType.PHASE,
                        phase=phase,
                        safe_payload={
                            "status": "failed",
                            "failure_category": RuntimeFailureCategory.UNKNOWN.value,
                        },
                    )
                )
            events.append(
                RuntimeTerminalEvent(
                    event_type=RuntimeEventType.RUN_FAILED,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "status": "failed",
                        "failure_category": RuntimeFailureCategory.UNKNOWN.value,
                    },
                )
            )
            await self.writer.submit_terminal(
                RuntimeTerminalCommit(
                    run_id=run_id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=lease_version,
                    expected_run_status=RuntimeRunStatus.RUNNING,
                    target_run_status=RuntimeRunStatus.FAILED,
                    expected_attempt_status=RuntimeAttemptStatus.RUNNING,
                    target_attempt_status=RuntimeAttemptStatus.FAILED,
                    events=tuple(events),
                    failure_category=RuntimeFailureCategory.UNKNOWN,
                )
            )

    async def _renew_loop(
        self,
        run_id: str,
        owner: str,
        lease_version: int,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                renewed = self.store.renew_lease(run_id, owner=owner, lease_version=lease_version)
                if renewed is None:
                    stop.set()
                    return

    def _validated_checkpoint(self, run: RuntimeRun):
        self.fault_injector.hit("checkpoint_tamper")
        if run.latest_checkpoint_id is None:
            raise RuntimeConflict("interrupted run has no complete checkpoint")
        checkpoint = self.store.get_checkpoint(run.latest_checkpoint_id)
        if checkpoint.run_id != run.id or checkpoint.schema_version != 1:
            raise RuntimeConflict("checkpoint schema or scope is invalid")
        checkpoints = self.store.list_checkpoints(run.id)
        if not checkpoints or checkpoints[-1].id != checkpoint.id:
            raise RuntimeConflict("checkpoint is not the latest complete checkpoint")
        if run.current_phase != checkpoint.completed_phase:
            raise RuntimeConflict("checkpoint phase does not match runtime position")
        attempt = self.store.get_attempt(checkpoint.attempt_id)
        if attempt.run_id != run.id or attempt.status != RuntimeAttemptStatus.INTERRUPTED:
            raise RuntimeConflict("checkpoint attempt is not interrupted")
        events = self.store.list_events(run.id, after=checkpoint.event_sequence - 1, limit=1)
        if (
            len(events) != 1
            or events[0].sequence != checkpoint.event_sequence
            or events[0].attempt_id != checkpoint.attempt_id
            or events[0].phase != checkpoint.completed_phase
            or events[0].event_type != RuntimeEventType.CHECKPOINT_CREATED
            or events[0].safe_payload.get("checkpoint_id") != checkpoint.id
        ):
            raise RuntimeConflict("checkpoint event sequence is invalid")
        expected = checkpoint_digest(
            run_id=checkpoint.run_id,
            attempt_id=checkpoint.attempt_id,
            completed_phase=checkpoint.completed_phase,
            resume_state=checkpoint.resume_state,
            schema_version=checkpoint.schema_version,
        )
        if checkpoint.state_digest != expected:
            raise RuntimeConflict("checkpoint digest is invalid")
        repository = getattr(self.store, "investigation_repository", None)
        if repository is None:
            raise RuntimeConflict("checkpoint business projection is unavailable")
        try:
            projection_digest = durable_projection_digest(
                repository=repository,
                investigation_id=run.investigation_id,
                run_id=run.id,
                resume_state=checkpoint.resume_state,
                require_exact=False,
            )
        except ValueError as exc:
            raise RuntimeConflict("checkpoint business projection is invalid") from exc
        if checkpoint.projection_digest != projection_digest:
            raise RuntimeConflict("checkpoint business projection digest is invalid")
        self._validate_recovery_projection(run, checkpoint)
        self._validate_references(run, checkpoint.resume_state)
        return checkpoint

    def _validate_recovery_projection(self, run: RuntimeRun, checkpoint) -> None:
        """只接受 checkpoint 投影或其后由本 Run Tool 终态事件证明的增量。"""
        repository = getattr(self.store, "investigation_repository", None)
        if repository is None:
            raise RuntimeConflict("checkpoint business projection is unavailable")
        state = checkpoint.resume_state
        record = repository.get(run.investigation_id)
        findings = repository.list_agent_findings(run.investigation_id)
        review = repository.get_coordination_review(run.investigation_id)
        report = record.report
        expected_findings = set(state.completed_finding_ids)
        expected_reviews = set(state.completed_review_ids)
        expected_reports = set(state.completed_report_ids)
        if (
            {item.id for item in findings} != expected_findings
            or ({review.id} if review is not None else set()) != expected_reviews
            or ({report.id} if report is not None else set()) != expected_reports
        ):
            raise RuntimeConflict("checkpoint has unauthorized phase projection")

        calls_by_id = {
            call.id: call
            for call in repository.list_tool_calls(run.investigation_id)
            if call.runtime_run_id == run.id
        }
        authorized_tool_keys: set[str] = set()
        authorized_evidence_ids: set[str] = set()
        for event in self.store.list_events(run.id, after=checkpoint.event_sequence, limit=500):
            if event.event_type != RuntimeEventType.TOOL_COMPLETED:
                continue
            call = calls_by_id.get(event.tool_call_id)
            if (
                call is None
                or call.status != ToolCallStatus.SUCCESS
                or call.idempotency_key is None
                or event.safe_payload.get("idempotency_key") != call.idempotency_key
                or set(event.evidence_ids) != set(call.output_evidence_ids)
            ):
                raise RuntimeConflict("checkpoint tool increment is invalid")
            authorized_tool_keys.add(call.idempotency_key)
            authorized_evidence_ids.update(call.output_evidence_ids)

        actual_evidence = {item.id for item in record.evidence}
        expected_evidence = set(state.completed_evidence_ids)
        actual_tool_keys = {
            call.idempotency_key
            for call in calls_by_id.values()
            if call.status == ToolCallStatus.SUCCESS and call.idempotency_key is not None
        }
        expected_tool_keys = set(state.successful_tool_keys)
        if actual_evidence != expected_evidence | authorized_evidence_ids:
            raise RuntimeConflict("checkpoint evidence increment is unauthorized")
        if actual_tool_keys != expected_tool_keys | authorized_tool_keys:
            raise RuntimeConflict("checkpoint tool increment is unauthorized")

    def _effective_resume_state(self, run: RuntimeRun, checkpoint) -> RuntimeResumeState:
        """把恢复基线与已落盘的计费结果合并，避免恢复时重置预算。"""
        repository = getattr(self.store, "investigation_repository", None)
        if repository is None:
            raise RuntimeConflict("checkpoint business projection is unavailable")
        resume_state = (
            checkpoint.resume_state
            if checkpoint is not None
            else RuntimeResumeState(
                remaining_tool_budget=run.tool_budget or 0,
                remaining_token_budget=run.token_budget,
            )
        )
        consumed_tools = durable_tool_call_count(
            repository=repository,
            investigation_id=run.investigation_id,
            run_id=run.id,
            mutation=None,
        )
        consumed_tokens = durable_token_usage(self.store.list_events(run.id), run.id)
        remaining_tool_budget = resume_state.remaining_tool_budget
        if run.tool_budget is not None:
            remaining_tool_budget = min(
                remaining_tool_budget,
                max(0, run.tool_budget - consumed_tools),
            )
        remaining_token_budget = resume_state.remaining_token_budget
        if run.token_budget is not None:
            remaining_token_budget = min(
                remaining_token_budget if remaining_token_budget is not None else 0,
                max(0, run.token_budget - consumed_tokens),
            )
        return resume_state.model_copy(
            update={
                "remaining_tool_budget": remaining_tool_budget,
                "remaining_token_budget": remaining_token_budget,
            }
        )

    def _validate_references(self, run: RuntimeRun, state: RuntimeResumeState) -> None:
        repository = getattr(self.store, "investigation_repository", None)
        if repository is None:
            return
        record = repository.get(run.investigation_id)
        if not set(state.completed_evidence_ids) <= {item.id for item in record.evidence}:
            raise RuntimeConflict("checkpoint evidence reference is invalid")
        if not set(state.completed_finding_ids) <= {
            item.id for item in repository.list_agent_findings(run.investigation_id)
        }:
            raise RuntimeConflict("checkpoint finding reference is invalid")
        review = repository.get_coordination_review(run.investigation_id)
        valid_review_ids = {review.id} if review is not None else set()
        if not set(state.completed_review_ids) <= valid_review_ids:
            raise RuntimeConflict("checkpoint review reference is invalid")
        valid_report_ids = {record.report.id} if record.report is not None else set()
        if not set(state.completed_report_ids) <= valid_report_ids:
            raise RuntimeConflict("checkpoint report reference is invalid")
        successful_keys = {
            call.idempotency_key
            for call in repository.list_tool_calls(run.investigation_id)
            if call.status == ToolCallStatus.SUCCESS
            and call.runtime_run_id == run.id
            and call.idempotency_key is not None
        }
        if not set(state.successful_tool_keys) <= successful_keys:
            raise RuntimeConflict("checkpoint successful tool key is invalid")
        if run.tool_budget is not None and state.remaining_tool_budget > run.tool_budget:
            raise RuntimeConflict("checkpoint tool budget exceeds frozen run config")
        if (
            run.token_budget is not None
            and state.remaining_token_budget is not None
            and state.remaining_token_budget > run.token_budget
        ):
            raise RuntimeConflict("checkpoint token budget exceeds frozen run config")

    def _resume_position(self, run: RuntimeRun) -> tuple[RuntimeResumeState, int]:
        if run.latest_checkpoint_id is None:
            return RuntimeResumeState(), 0
        checkpoint = self._validated_checkpoint(run)
        return (
            checkpoint.resume_state,
            RUNTIME_PHASE_ORDER.index(checkpoint.completed_phase) + 1,
        )

    def _running_attempt(self, run_id: str) -> RuntimeAttempt:
        attempts = self.store.list_attempts(run_id)
        for attempt in reversed(attempts):
            if attempt.status == RuntimeAttemptStatus.RUNNING:
                return attempt
        raise RuntimeConflict("runtime run has no active attempt")
