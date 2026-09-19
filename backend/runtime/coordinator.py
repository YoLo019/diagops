from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from backend.domain.multi_agent import FailureCategory
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
    BusinessMutation,
    PhaseCommit,
    PhaseInput,
    ToolCommit,
    checkpoint_digest,
    durable_model_reservations,
    durable_projection_digest,
    durable_remaining_token_budget,
    durable_tool_call_count,
    phase_profile_for,
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


class _RuntimeDeadlineExceeded(Exception):
    """表示 Run 的绝对 deadline 已到，禁止继续产生业务提交。"""


def _escape_failure_category(exc: BaseException) -> RuntimeFailureCategory:
    """逃逸出 phase 的模型输出契约违约映射为显式类别，其余保持 UNKNOWN。

    延迟 import：runtime 基础设施层不应对 diagnosis/reports 形成模块级依赖。
    """
    from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
    from backend.diagnosis.result_validation import V11ResultValidationError
    from backend.diagnosis.v11_runtime import V11RuntimeContractError, _investigator_failure_audit
    from backend.reports.generator import ReportReferenceError

    if isinstance(exc, V11RuntimeContractError) and str(exc).startswith(
        "structured correction exhausted"
    ):
        return RuntimeFailureCategory.OUTPUT_VALIDATION
    if (
        isinstance(exc, V11RuntimeContractError)
        and _investigator_failure_audit(exc)[1] == FailureCategory.QUOTA
    ):
        return RuntimeFailureCategory.MODEL_FAILURE
    if isinstance(
        exc,
        (V11ResultValidationError, V11RuntimeContractError, ReportReferenceError),
    ):
        return RuntimeFailureCategory.CONTRACT_INTEGRITY
    if isinstance(exc, ClassifiedRetryableError) and exc.category is FailureCategory.TIMEOUT:
        # 模型调用重试耗尽后从无 turn 级 catch 的 phase 逃逸的超时：与 run
        # deadline 的 TIMEOUT 同属运营噪声，归一以保持案例级有界重试的
        # phase 中立性。
        return RuntimeFailureCategory.TIMEOUT
    return RuntimeFailureCategory.UNKNOWN


def _execution_failure_category(executions, run_id: str) -> RuntimeFailureCategory:
    """阶段正常返回 failed 时，保留尚未被成功重试解决的模型失败类别。"""
    from backend.diagnosis.v11_runtime import _resolved_execution_ids

    executions = [item for item in executions if item.runtime_run_id == run_id]
    resolved = _resolved_execution_ids(executions)
    categories = {
        item.failure_category for item in executions
        if item.status.value == "failed" and item.id not in resolved
    }
    if FailureCategory.TIMEOUT in categories:
        return RuntimeFailureCategory.TIMEOUT
    if categories & {FailureCategory.QUOTA, FailureCategory.TRANSPORT, FailureCategory.RATE_LIMIT}:
        return RuntimeFailureCategory.MODEL_FAILURE
    if FailureCategory.INVALID_REFERENCE in categories:
        return RuntimeFailureCategory.CONTRACT_INTEGRITY
    return RuntimeFailureCategory.OUTPUT_VALIDATION


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
        self._deadlines: dict[str, float] = {}
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
            if first_request and active is not None and run.status == RuntimeRunStatus.CANCELLING:
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
        if run.is_v11:
            deadline = self._deadlines.get(run_id)
            if deadline is None:
                deadline = self._deadline_for(run)
                self._deadlines[run_id] = deadline
            if time.monotonic() >= deadline:
                raise _RuntimeDeadlineExceeded
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
            if run.is_v11:
                self._deadlines[run.id] = self._deadline_for(run)
            self.check_execution(run.id, owner, run.lease_version)
            await self._emit_start(run, attempt, owner)
            if recovery:
                await self._emit_recovery_events(
                    run,
                    attempt,
                    owner,
                    recovery_checkpoint_id,
                )
                await self._reconcile_model_reservations(run, attempt, owner)
                # 首个 checkpoint 前也可能已产生远端费用，必须从事件恢复预算。
                recovery_state = self._effective_resume_state(
                    run,
                    self.store.get_checkpoint(recovery_checkpoint_id)
                    if recovery_checkpoint_id is not None
                    else None,
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
            phase_profile = phase_profile_for(run.execution_contract_version)
            for phase in phase_profile.order[start_index:]:
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
                        execution_contract_version=run.execution_contract_version,
                        execution_contract=run.execution_contract,
                        model_provider=run.model_provider,
                        model_name=run.model_name,
                        prompt_version=run.prompt_version,
                        tool_budget=available_tool_budget,
                        token_budget=available_token_budget,
                        timeout_seconds=run.timeout_seconds,
                        deadline_at=(run.started_at or run.created_at)
                        + timedelta(seconds=run.timeout_seconds),
                        remaining_deadline_seconds=(
                            lambda run_id=run.id: self._remaining_deadline_seconds(run_id)
                        ),
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
                        safe_payload=None,
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
                                safe_payload,
                            )
                        ),
                        reserve_model_turn=(
                            lambda run_id=run.id, owner=owner, lease_version=run.lease_version: (
                                self.store.reserve_model_turn(
                                    run_id,
                                    owner=owner,
                                    lease_version=lease_version,
                                )
                            )
                        ),
                        model_events=tuple(self.store.list_events(run.id, limit=10_000)),
                        hit_fault=self.fault_injector.hit,
                    )
                )
                self.check_execution(run.id, owner, run.lease_version)
                await self._close_open_phase_agents(run, attempt, owner, phase)
                output = self._merge_committed_tools(run, output)
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
                if (
                    run.is_v11
                    and self.store.investigation_repository.get(run.investigation_id).status.value
                    == "failed"
                ):
                    await self._fail_if_owned(
                        run.id,
                        attempt,
                        owner,
                        run.lease_version,
                        # failed 的 PhaseCommit 已记录 phase.failed 事件，避免重复。
                        phase=phase if output.status != "failed" else None,
                        failure_category=_execution_failure_category(
                            self.store.investigation_repository.list_executions(run.investigation_id),
                            run.id,
                        ),
                    )
                    return self.store.get_run(run.id)
            if (
                run.is_v11
                and self.store.investigation_repository.get(run.investigation_id).status.value
                == "failed"
            ):
                await self._fail_if_owned(
                    run.id,
                    attempt,
                    owner,
                    run.lease_version,
                    failure_category=_execution_failure_category(
                        self.store.investigation_repository.list_executions(run.investigation_id),
                        run.id,
                    ),
                )
                return self.store.get_run(run.id)
            await self._complete(run, attempt, owner)
            return self.store.get_run(run.id)
        except _RuntimeCancellationRequested:
            await self._finish_cancel(run.id, attempt, owner, run.lease_version)
            return self.store.get_run(run.id)
        except _RuntimeDeadlineExceeded:
            # CANCELLING 的 Run 遇到 deadline 也要收敛终态：先按取消收尾，
            # 仍为 RUNNING 时再按 TIMEOUT 失败提交；两者互斥且各自幂等。
            await self._finish_cancel(run.id, attempt, owner, run.lease_version)
            await self._fail_if_owned(
                run.id,
                attempt,
                owner,
                run.lease_version,
                phase=active_phase,
                failure_category=RuntimeFailureCategory.TIMEOUT,
            )
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
            # 真实持久化失败不是可发布的运行结果。先用同一 lease 尝试写入
            # 明确的 failed 终态；如果数据库仍不可写，则保留 running 交给
            # lease audit/recovery，不能用本地异常覆盖 durable 状态。
            try:
                await self._fail_if_owned(
                    run.id,
                    attempt,
                    owner,
                    run.lease_version,
                    phase=active_phase,
                    failure_category=RuntimeFailureCategory.PERSISTENCE_FAILURE,
                )
            except (RuntimeConflict, RuntimeLeaseLost, RuntimePersistenceError):
                logger.warning(
                    "runtime persistence failure could not be terminalized "
                    "run_id=%s exception_type=%s",
                    run.id,
                    type(exc).__name__,
                )
            raise
        except Exception as exc:
            self.fault_injector.hit("parallel_session_failure")
            await self._fail_if_owned(
                run.id,
                attempt,
                owner,
                run.lease_version,
                phase=active_phase,
                failure_category=_escape_failure_category(exc),
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
            self._deadlines.pop(run.id, None)

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
            self.check_execution(run.id, owner, run.lease_version)
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

    def _merge_committed_tools(self, run, output):
        """保留 SDK 取消等待前已完成的工具事务，避免阶段快照删除成功证据。"""
        mutation = output.business_mutation
        if not run.is_v11 or mutation.activate_projection or mutation.investigation is None:
            return output
        repository = self.store.investigation_repository
        current = repository.get(run.investigation_id)
        merged = mutation.merge_tool_result(current).investigation
        record = mutation.investigation.model_copy(update={
            "evidence": merged.evidence,
            "provider_results": merged.provider_results,
        })
        calls = {item.id: item for item in mutation.tool_calls or ()}
        calls.update({
            item.id: item for item in repository.list_tool_calls(run.investigation_id)
            if item.runtime_run_id == run.id
        })
        mutation = replace(mutation, investigation=record, tool_calls=tuple(calls.values()))
        consumed = durable_tool_call_count(
            repository=repository, investigation_id=run.investigation_id,
            run_id=run.id, mutation=mutation,
        )
        resume_state = output.resume_state.model_copy(update={
            "completed_evidence_ids": [item.id for item in record.evidence],
            "successful_tool_keys": sorted({
                item.idempotency_key for item in calls.values()
                if item.status == ToolCallStatus.SUCCESS and item.idempotency_key is not None
            }),
            "remaining_tool_budget": max(0, run.tool_budget - consumed)
            if run.tool_budget is not None else output.resume_state.remaining_tool_budget,
        })
        payload = dict(output.safe_payload)
        if "evidence_count" in payload:
            payload["evidence_count"] = len(record.evidence)
        return replace(
            output, business_mutation=mutation, resume_state=resume_state, safe_payload=payload,
        )

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
        provider_results = {
            item.model_dump_json(): item
            for item in [*record.provider_results, *result.provider_results]
        }
        record = record.model_copy(
            update={
                "evidence": list(evidence.values()),
                "provider_results": list(provider_results.values()),
                "updated_at": datetime.now(UTC),
            }
        )
        span = self._tool_spans.pop((attempt.id, result.call.id), None)
        try:
            self.check_execution(run.id, owner, run.lease_version)
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
        safe_payload: dict[str, object] | None = None,
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
                        **(safe_payload or {}),
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

    async def _reconcile_model_reservations(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
    ) -> None:
        """恢复时释放崩溃窗口中的未结算 reservation，保证幂等续跑。"""
        pending = durable_model_reservations(self.store.list_events(run.id), run.id)
        for reservation_id, details in sorted(pending.items()):
            self.check_execution(run.id, owner, run.lease_version)
            logical_call_id = details.get("logical_call_id")
            reserved_tokens = int(details.get("reserved_tokens", 0))
            input_estimate = int(details.get("input_estimate", 0))
            await self.writer.submit_event(
                RuntimeEventCommand(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    lease_version=run.lease_version,
                    event_type=RuntimeEventType.MODEL_FAILED,
                    actor_type=RuntimeActorType.MODEL,
                    phase=run.current_phase,
                    actor_name="CoordinatorAgent",
                    execution_id=details.get("execution_id"),
                    safe_payload={
                        "status": "failed",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "logical_call_id": logical_call_id,
                        "reservation_id": reservation_id,
                        "reservation_status": "unknown",
                        "usage_known": False,
                        "accounted_tokens": reserved_tokens,
                        "reserved_tokens": reserved_tokens,
                        "input_estimate": input_estimate,
                        "attempt": attempt.attempt_number,
                    },
                )
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
        if status != "started":
            await self._close_running_agent_tools(
                run,
                attempt,
                owner,
                phase,
                actor_name,
                status,
            )
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

    async def _close_open_phase_agents(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
    ) -> None:
        """Phase 终态前关闭未上报终态的 Agent 及其 Tool 子生命周期。"""
        for attempt_id, actor_name in list(self._agent_spans):
            if attempt_id == attempt.id:
                await self._persist_agent_event(run, attempt, owner, phase, actor_name, "failed")

    async def _close_running_agent_tools(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        owner: str,
        phase: RuntimePhase,
        actor_name: str,
        agent_status: str,
    ) -> None:
        """Agent 终态前收口 SDK 遗留 Tool，保证父生命周期不会越过仍在 running 的子调用。"""
        calls = self.store.investigation_repository.list_tool_calls(run.investigation_id)
        for call in calls:
            if (
                call.runtime_run_id != run.id
                or call.agent_name != actor_name
                or call.status != ToolCallStatus.RUNNING
            ):
                continue
            completed_at = datetime.now(UTC)
            started_at = call.started_at or completed_at
            interrupted = call.model_copy(
                update={
                    "status": ToolCallStatus.INTERRUPTED,
                    "completed_at": completed_at,
                    "duration_ms": max(
                        0,
                        int((completed_at - started_at).total_seconds() * 1000),
                    ),
                    "error_message": f"agent {agent_status}",
                }
            )
            try:
                await self._persist_tool_result(
                    run,
                    attempt,
                    owner,
                    phase,
                    ToolInvocationResult(
                        call=interrupted,
                        evidence=[],
                        provider_results=[],
                    ),
                )
            except RuntimeConflict:
                current = next(
                    (
                        item
                        for item in self.store.investigation_repository.list_tool_calls(
                            run.investigation_id
                        )
                        if item.id == call.id
                    ),
                    None,
                )
                if current is None or current.status not in {
                    ToolCallStatus.SUCCESS,
                    ToolCallStatus.FAILED,
                    ToolCallStatus.INTERRUPTED,
                }:
                    raise

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
        failure_category: RuntimeFailureCategory = RuntimeFailureCategory.UNKNOWN,
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
                            "failure_category": failure_category.value,
                        },
                    )
                )
            events.append(
                RuntimeTerminalEvent(
                    event_type=RuntimeEventType.RUN_FAILED,
                    actor_type=RuntimeActorType.RUNTIME,
                    safe_payload={
                        "status": "failed",
                        "failure_category": failure_category.value,
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
                    failure_category=failure_category,
                )
            )

    @staticmethod
    def _deadline_for(run: RuntimeRun) -> float:
        started = run.started_at or run.created_at
        elapsed = max(0.0, (datetime.now(UTC) - started).total_seconds())
        return time.monotonic() + max(0.0, run.timeout_seconds - elapsed)

    def _remaining_deadline_seconds(self, run_id: str) -> float:
        deadline = self._deadlines.get(run_id)
        if deadline is None:
            deadline = self._deadline_for(self.store.get_run(run_id))
            self._deadlines[run_id] = deadline
        return max(0.0, deadline - time.monotonic())

    async def _renew_loop(
        self,
        run_id: str,
        owner: str,
        lease_version: int,
        stop: asyncio.Event,
    ) -> None:
        wait_seconds = self.heartbeat_seconds
        retry_seconds = min(max(self.heartbeat_seconds / 4, 0.1), 1.0)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait_seconds)
            except TimeoutError:
                try:
                    renewed = await asyncio.to_thread(
                        self.store.renew_lease,
                        run_id,
                        owner=owner,
                        lease_version=lease_version,
                    )
                except RuntimePersistenceError:
                    # 续租失败不能让后台 task 静默退出；SQLite 短暂 busy 时先快速重试，
                    # 只有 CAS 返回 None 才确认 lease 已丢失并停止执行。
                    logger.warning(
                        "runtime lease renewal deferred run_id=%s "
                        "category=runtime_lease_persistence",
                        run_id,
                    )
                    wait_seconds = retry_seconds
                    continue
                wait_seconds = self.heartbeat_seconds
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
                remaining_model_turns=run.remaining_model_turns,
            )
        )
        consumed_tools = durable_tool_call_count(
            repository=repository,
            investigation_id=run.investigation_id,
            run_id=run.id,
            mutation=None,
        )
        durable_events = self.store.list_events(run.id)
        remaining_from_events = (
            durable_remaining_token_budget(
                durable_events,
                run.id,
                run.token_budget,
            )
            if run.token_budget is not None
            else None
        )
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
                remaining_from_events or 0,
            )
        remaining_model_turns = resume_state.remaining_model_turns
        if run.is_v11:
            if run.remaining_model_turns is None:
                raise RuntimeConflict("V11 run lacks durable model turn budget")
            if remaining_model_turns is None:
                remaining_model_turns = run.remaining_model_turns
            else:
                remaining_model_turns = min(
                    remaining_model_turns,
                    run.remaining_model_turns,
                )
        return resume_state.model_copy(
            update={
                "remaining_tool_budget": remaining_tool_budget,
                "remaining_token_budget": remaining_token_budget,
                "remaining_model_turns": remaining_model_turns,
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
        if run.is_v11 and (
            state.remaining_model_turns is None
            or run.remaining_model_turns is None
            or state.remaining_model_turns < run.remaining_model_turns
        ):
            raise RuntimeConflict("checkpoint model turn budget is below durable run state")

    def _resume_position(self, run: RuntimeRun) -> tuple[RuntimeResumeState, int]:
        if run.latest_checkpoint_id is None:
            return (
                RuntimeResumeState(
                    remaining_tool_budget=run.tool_budget or 0,
                    remaining_token_budget=run.token_budget,
                    remaining_model_turns=run.remaining_model_turns,
                ),
                0,
            )
        checkpoint = self._validated_checkpoint(run)
        order = phase_profile_for(run.execution_contract_version).order
        if checkpoint.completed_phase not in order:
            raise RuntimeConflict("checkpoint phase belongs to another execution profile")
        return (
            checkpoint.resume_state,
            order.index(checkpoint.completed_phase) + 1,
        )

    def _running_attempt(self, run_id: str) -> RuntimeAttempt:
        attempts = self.store.list_attempts(run_id)
        for attempt in reversed(attempts):
            if attempt.status == RuntimeAttemptStatus.RUNNING:
                return attempt
        raise RuntimeConflict("runtime run has no active attempt")
