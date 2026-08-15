from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from agents import FunctionTool
from agents.exceptions import ModelBehaviorError
from openai import APIConnectionError, RateLimitError

from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, JsonValue
from backend.domain.multi_agent import AdaptiveStopReason, FailureCategory
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.domain.tool_queries import (
    DependencyQuery,
    MemoryQuery,
    QueryWindow,
    ScopedTelemetryQuery,
)
from backend.providers.results import ProviderResult, ProviderStatus
from backend.runtime.concurrency import RunStepGate
from backend.safety.redaction import redact_value
from backend.tools.provider_tools import QUERY_MODELS_BY_TOOL
from backend.tools.registry import ToolInvocationResult, ToolRegistry

TOOLS_BY_AGENT = {
    AgentName.LOG: frozenset({"read_logs"}),
    AgentName.METRIC: frozenset({"query_metrics", "query_prometheus"}),
    AgentName.DEPLOYMENT: frozenset(
        {"read_deployments", "read_service_catalog", "query_dependencies"}
    ),
}
AgentIdentity = AgentName | str


class ClassifiedRetryableError(RuntimeError):
    """Provider 已把异常分类为可重试 transport/rate-limit。"""

    def __init__(self, category: FailureCategory) -> None:
        self.category = category
        super().__init__(category.value)


def retryable_failure_category(exc: BaseException) -> FailureCategory | None:
    """只把明确的 transport/rate-limit/畸形输出异常交给统一 retry coordinator。

    裸 TimeoutError 不在此处放行：工具路径的超时契约是不重试（有测试围栏）；
    模型调用的超时由 v11_runtime 在持久化后包装为 ClassifiedRetryableError
    再进入本函数。
    """
    if isinstance(exc, RateLimitError):
        return FailureCategory.RATE_LIMIT
    if isinstance(exc, APIConnectionError):
        return FailureCategory.TRANSPORT
    if isinstance(exc, ModelBehaviorError):
        # 模型/网关输出畸形（JSON 垃圾、schema 漂移）：重试一次换一批生成，
        # 不再让单次畸形响应直接杀 run。
        return FailureCategory.INVALID_OUTPUT
    if isinstance(exc, ClassifiedRetryableError):
        return exc.category
    return None


class RetryBudgetRejected(RuntimeError):
    """重试前的 durable budget/deadline/cancel fence 拒绝了下一次尝试。"""


class ActionDeadlineExceeded(RuntimeError):
    """工具 action 在 absolute deadline 前没有可用启动窗口。"""


class RetryCoordinator:
    """为 model/tool provider 调用提供有界、分类明确、可带退避的 retry。"""

    def __init__(
        self,
        *,
        max_retries: int = 1,
        backoff_base_seconds: float = 0.0,
        backoff_cap_seconds: float = 0.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base_seconds < 0 or backoff_cap_seconds < 0:
            raise ValueError("backoff must be non-negative")
        if backoff_base_seconds > 0 and backoff_cap_seconds == 0:
            # min(cap=0, ...) 恒为 0，等价于静默关闭退避；要求显式传 cap。
            raise ValueError("backoff_cap_seconds must be positive when base is set")
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds

    async def run(
        self,
        operation: Callable[[int], Any],
        *,
        before_retry: Callable[[int, FailureCategory], Awaitable[None] | None]
        | None = None,
    ) -> Any:
        attempt = 1
        while True:
            try:
                result = operation(attempt)
                if inspect.isawaitable(result):
                    return await result
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = retryable_failure_category(exc)
                if category is None or attempt > self.max_retries:
                    raise
                if self.backoff_base_seconds > 0:
                    # 指数退避吸收网关抖动窗口；sleep 可取消，预算/死线栅栏
                    # 由 before_retry 在退避后、下次尝试前重新把关。
                    await asyncio.sleep(
                        min(
                            self.backoff_cap_seconds,
                            self.backoff_base_seconds * 2 ** (attempt - 1),
                        )
                    )
                if before_retry is not None:
                    await _maybe_await(before_retry(attempt, category))
                attempt += 1


class AdaptiveToolSession:
    """维护单次 Investigation 的只读工具权限、预算和新增证据。"""

    def __init__(
        self,
        *,
        event: IncidentEvent,
        seed_evidence: list[EvidenceItem],
        registry: ToolRegistry,
        task_ids: dict[object, str],
        max_tool_calls_per_specialist: int = 3,
        max_total_tool_calls: int = 8,
        tool_timeout_seconds: float = 10,
        allowed_targets: set[str] | None = None,
        runtime_run_id: str | None = None,
        resolve_tool_result: Callable[[str], ToolCallRecord | None] | None = None,
        persist_tool_start: Callable[[ToolCallRecord], Awaitable[ToolCallRecord]]
        | None = None,
        persist_tool_result: Callable[
            [ToolInvocationResult], Awaitable[ToolCallRecord]
        ]
        | None = None,
        check_execution: Callable[[], None] | None = None,
        max_parallel_steps_per_run: int = 3,
        hit_fault: Callable[[str], None] | None = None,
        parallel_limit: RunStepGate | None = None,
        agent_manifest: tuple[str, ...] | None = None,
        remaining_deadline_seconds: Callable[[], float] | None = None,
    ) -> None:
        if max_parallel_steps_per_run < 1:
            raise ValueError("max_parallel_steps_per_run must be positive")
        self.event = event
        self.registry = registry
        self.task_ids = task_ids
        self.agent_manifest = agent_manifest
        self.max_tool_calls_per_specialist = max_tool_calls_per_specialist
        self.max_total_tool_calls = max_total_tool_calls
        self.tool_timeout_seconds = tool_timeout_seconds
        self._remaining_deadline_seconds = remaining_deadline_seconds or (
            lambda: float("inf")
        )
        self.runtime_run_id = runtime_run_id
        self._resolve_tool_result = resolve_tool_result or (lambda _key: None)
        self._persist_tool_start = persist_tool_start
        self._persist_tool_result = persist_tool_result
        self._check_execution = check_execution or (lambda: None)
        self._parallel_limit = parallel_limit or RunStepGate(
            max_parallel_steps_per_run
        )
        self._hit_fault = hit_fault or (lambda _point: None)
        self.tool_calls: list[ToolCallRecord] = []
        self.provider_results: list[ProviderResult] = []
        self.new_evidence: list[EvidenceItem] = []
        self.stop_reasons: dict[AgentIdentity, AdaptiveStopReason] = {}
        self._attempts: dict[AgentIdentity, int] = {}
        self._total_attempts = 0
        self._fingerprints: set[str] = set()
        self._known_evidence_ids = {item.id for item in seed_evidence}
        self._stopped_agents: set[AgentIdentity] = set()
        self._allowed_targets = {event.service, *(allowed_targets or set())}
        for item in seed_evidence:
            dependencies = item.payload.get("dependencies", [])
            if isinstance(dependencies, list):
                self._allowed_targets.update(
                    value for value in dependencies if isinstance(value, str)
                )
            edges = item.payload.get("edges", [])
            if isinstance(edges, list):
                self._allowed_targets.update(
                    value
                    for edge in edges
                    if isinstance(edge, dict)
                    for value in (edge.get("parent"), edge.get("child"))
                    if isinstance(value, str)
                )

    def tools_for(
        self, agent_name: AgentIdentity, round_number: int, attempt: int = 1
    ) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        tool_names = (
            self.agent_manifest
            if self.agent_manifest is not None
            else tuple(sorted(TOOLS_BY_AGENT[agent_name]))
        )
        for tool_name in tool_names:
            spec = (
                self.registry.assert_agent_callable(tool_name, self.agent_manifest)
                if self.agent_manifest is not None
                else self.registry.get(tool_name)
            )
            if not spec.read_only:
                raise ValueError(f"adaptive tool must be read-only: {tool_name}")

            async def invoke(
                _context,
                raw_input: str,
                *,
                name: str = tool_name,
                agent: AgentName = agent_name,
            ):
                return await self.invoke(
                    agent, name, raw_input, round_number, attempt=attempt
                )

            tools.append(
                FunctionTool(
                    name=spec.name,
                    description=spec.description,
                    params_json_schema=dict(spec.input_schema),
                    on_invoke_tool=invoke,
                    strict_json_schema=True,
                    timeout_seconds=self.tool_timeout_seconds + 1,
                    timeout_behavior="error_as_result",
                )
            )
        return tools

    async def invoke(
        self,
        agent_name: AgentIdentity,
        tool_name: str,
        raw_input: str,
        round_number: int,
        attempt: int = 1,
    ) -> str:
        try:
            self._action_timeout()
        except ActionDeadlineExceeded:
            return _response(
                status=ToolCallStatus.FAILED,
                evidence=[],
                warning="tool deadline exhausted before start",
                stop_reason=AdaptiveStopReason.TIMEOUT,
            )
        task_id = self.task_id_for(agent_name, round_number, attempt)
        parsed = self._parse_input(raw_input)
        self._attempts[agent_name] = self._attempts.get(agent_name, 0) + 1
        self._total_attempts += 1

        if parsed is None:
            return self._reject(
                agent_name,
                tool_name,
                {},
                ToolCallStatus.FAILED,
                "invalid tool input",
                task_id=task_id,
            )
        if self.agent_manifest is not None:
            try:
                self.registry.assert_agent_callable(tool_name, self.agent_manifest)
            except ValueError:
                return self._reject(
                    agent_name,
                    tool_name,
                    parsed,
                    ToolCallStatus.FAILED,
                    "tool not in frozen agent manifest",
                    task_id=task_id,
                )
            allowed_tools = self.agent_manifest
        else:
            allowed_tools = TOOLS_BY_AGENT[agent_name]
        if tool_name not in allowed_tools:
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                "tool not allowed for agent",
                task_id=task_id,
            )

        try:
            query = QUERY_MODELS_BY_TOOL[tool_name].model_validate(parsed)
            self._validate_scope(query)
        except (KeyError, TypeError, ValueError):
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                "tool input outside investigation scope",
                task_id=task_id,
            )

        fingerprint = _query_fingerprint(tool_name, query)
        normalized_input = query.model_dump(mode="json", exclude={"reason"})
        safe_normalized_input = {
            key: value for key, value in normalized_input.items() if value is not None
        }
        logical_step = f"{_agent_value(agent_name)}:{round_number}:{attempt}"
        logical_call_id = (
            f"{_agent_value(agent_name)}:{round_number}:{attempt}:{fingerprint}"
        )
        idempotency_key = tool_idempotency_key(
            run_id=self.runtime_run_id,
            agent_name=agent_name,
            logical_step=logical_step,
            tool_name=tool_name,
            normalized_input=normalized_input,
        )
        # durable success 不再跨外部边界，也不应被恢复后的剩余预算阻断。
        self._check_execution()
        committed = self._resolve_tool_result(idempotency_key)
        if committed is not None and committed.status == ToolCallStatus.SUCCESS:
            reused = ToolCallRecord.model_validate(
                committed.model_dump(mode="python")
            )
            self.tool_calls.append(reused)
            return _response(
                status=ToolCallStatus.SUCCESS,
                evidence=[],
                warning=None,
                stop_reason=None,
            )
        if fingerprint in self._fingerprints:
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "duplicate query",
                AdaptiveStopReason.DUPLICATE_QUERY,
                task_id=task_id,
            )
        if agent_name in self._stopped_agents:
            reason = self.stop_reasons[agent_name]
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "specialist query stopped",
                reason,
                task_id=task_id,
            )
        if (
            self._attempts[agent_name] > self.max_tool_calls_per_specialist
            or self._total_attempts > self.max_total_tool_calls
        ):
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "adaptive tool budget exhausted",
                AdaptiveStopReason.BUDGET_EXHAUSTED,
                task_id=task_id,
            )

        self._fingerprints.add(fingerprint)
        current_call: ToolCallRecord | None = None

        async def invoke_attempt(retry_index: int) -> ToolInvocationResult:
            nonlocal current_call
            timeout_seconds = self._action_timeout()
            attempt_number = attempt + retry_index - 1
            attempt_task_id = self.task_id_for(
                agent_name, round_number, attempt_number
            )
            attempt_step = f"{_agent_value(agent_name)}:{round_number}:{attempt_number}"
            attempt_key = tool_idempotency_key(
                run_id=self.runtime_run_id,
                agent_name=agent_name,
                logical_step=attempt_step,
                tool_name=tool_name,
                normalized_input=normalized_input,
            )
            current_call = ToolCallRecord(
                id=f"tool-{uuid4().hex}",
                task_id=attempt_task_id,
                agent_name=_agent_value(agent_name),
                tool_name=tool_name,
                input=safe_normalized_input,
                status=ToolCallStatus.RUNNING,
                started_at=datetime.now(UTC),
                runtime_run_id=self.runtime_run_id,
                logical_call_id=logical_call_id,
                idempotency_key=attempt_key,
                execution_id=f"tool-exec-{uuid4().hex}",
                attempt=attempt_number,
            )
            if self._persist_tool_start is not None:
                await self._persist_tool_start(current_call)
                self._check_execution()
            timeout_seconds = self._action_timeout()
            async with self._parallel_limit.slot():
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.registry.invoke_detailed,
                        tool_name,
                        event=self.event,
                        task_id=attempt_task_id,
                        agent_name=_agent_value(agent_name),
                        input=query.model_dump(mode="json"),
                    ),
                    timeout=timeout_seconds,
                )
            self._action_timeout()
            for provider_result in result.provider_results:
                if provider_result.failure_category in {
                    FailureCategory.RATE_LIMIT,
                    FailureCategory.TRANSPORT,
                }:
                    raise ClassifiedRetryableError(provider_result.failure_category)
            return result

        async def before_retry(
            _retry_index: int, category: FailureCategory
        ) -> None:
            assert current_call is not None
            # 旧 attempt 先落 durable terminal，再检查下一 attempt 的共享预算。
            self._check_execution()
            self._action_timeout()
            if self.agent_manifest is not None:
                self.registry.assert_agent_callable(tool_name, self.agent_manifest)
            await self._finish_failed_invocation(
                current_call,
                f"retryable provider failure: {category.value}",
                None,
            )
            if (
                self._attempts[agent_name] > self.max_tool_calls_per_specialist
                or self._total_attempts > self.max_total_tool_calls
            ):
                raise RetryBudgetRejected("adaptive retry budget exhausted")

        retry_coordinator = RetryCoordinator(max_retries=1)
        try:
            result = await retry_coordinator.run(
                invoke_attempt,
                before_retry=before_retry,
            )
        except asyncio.CancelledError:
            if current_call is not None and current_call.status == ToolCallStatus.RUNNING:
                await self._finish_failed_invocation(
                    current_call,
                    "tool invocation cancelled",
                    None,
                    status=ToolCallStatus.INTERRUPTED,
                )
            raise
        except TimeoutError:
            return await self._finish_failed_invocation(
                current_call,
                "tool invocation timeout",
                AdaptiveStopReason.TIMEOUT,
            )
        except ActionDeadlineExceeded:
            return await self._finish_failed_invocation(
                current_call,
                "tool deadline exhausted",
                AdaptiveStopReason.TIMEOUT,
            )
        except RetryBudgetRejected:
            self._stop(agent_name, AdaptiveStopReason.BUDGET_EXHAUSTED)
            return _response(
                status=ToolCallStatus.FAILED,
                evidence=[],
                warning="adaptive retry budget exhausted",
                stop_reason=AdaptiveStopReason.BUDGET_EXHAUSTED,
            )
        except Exception:
            return await self._finish_failed_invocation(
                current_call,
                "tool invocation failed",
                None,
            )
        # 同步 Tool 在线程中完成后必须重新校验 lease/cancel fence，晚到结果不得推进状态。
        self._check_execution()
        if current_call is None:
            raise RuntimeError("tool attempt lacks a durable call")
        tool_call_id = current_call.id
        logical_call_id = current_call.logical_call_id
        idempotency_key = current_call.idempotency_key
        execution_id = current_call.execution_id
        owned_evidence = [
            item.model_copy(update={"runtime_run_id": self.runtime_run_id})
            if self.runtime_run_id is not None and item.runtime_run_id is None
            else item
            for item in result.evidence
        ]
        result = ToolInvocationResult(
            call=result.call.model_copy(
                update={
                "id": tool_call_id,
                "input": safe_normalized_input,
                "runtime_run_id": self.runtime_run_id,
                "logical_call_id": logical_call_id,
                "idempotency_key": idempotency_key,
                "execution_id": execution_id,
                "attempt": current_call.attempt,
                }
            ),
            evidence=owned_evidence,
            provider_results=list(result.provider_results),
        )
        if self._persist_tool_result is not None:
            persisted = await self._persist_tool_result(result)
            result = ToolInvocationResult(
                call=ToolCallRecord.model_validate(
                    persisted.model_dump(mode="python")
                ),
                evidence=result.evidence,
                provider_results=result.provider_results,
            )
        self._check_execution()
        self.tool_calls.append(result.call)
        self.provider_results.extend(result.provider_results)

        output_ids = set(result.call.output_evidence_ids)
        new_output_ids = output_ids - self._known_evidence_ids
        for item in result.evidence:
            if item.id in self._known_evidence_ids:
                continue
            self._known_evidence_ids.add(item.id)
            self.new_evidence.append(item)

        stop_reason = None
        if not new_output_ids and result.call.status != ToolCallStatus.FAILED:
            stop_reason = AdaptiveStopReason.NO_NEW_EVIDENCE
            self._stop(agent_name, stop_reason)

        return _response(
            status=result.call.status,
            evidence=project_tool_evidence(result.evidence),
            warning=_result_warning(result.provider_results),
            stop_reason=stop_reason,
        )

    async def _finish_failed_invocation(
        self,
        running_call: ToolCallRecord | None,
        message: str,
        stop_reason: AdaptiveStopReason | None,
        *,
        status: ToolCallStatus = ToolCallStatus.FAILED,
    ) -> str:
        """用同一逻辑调用身份收口 running，避免取消或超时后的晚到结果产生第二条记录。"""
        if running_call is None:
            return _response(
                status=status,
                evidence=[],
                warning=message,
                stop_reason=stop_reason,
            )
        completed_at = datetime.now(UTC)
        started_at = running_call.started_at or completed_at
        failed_call = running_call.model_copy(
            update={
                "status": status,
                "error_message": message,
                "completed_at": completed_at,
                "duration_ms": max(
                    0, int((completed_at - started_at).total_seconds() * 1000)
                ),
            }
        )
        if self._persist_tool_result is not None:
            persistence = asyncio.ensure_future(
                self._persist_tool_result(
                    ToolInvocationResult(
                        call=failed_call,
                        evidence=[],
                        provider_results=[],
                    )
                )
            )
            cancelled = False
            # SDK 可能在内部 timeout 收口期间再次 cancel；终止事件必须先于取消向上传播。
            while not persistence.done():
                try:
                    await asyncio.shield(persistence)
                except asyncio.CancelledError:
                    cancelled = True
            failed_call = persistence.result()
            if cancelled:
                raise asyncio.CancelledError
        self.tool_calls.append(failed_call)
        if stop_reason is not None:
            self._stop(running_call.agent_name, stop_reason)
        return _response(
            status=status,
            evidence=[],
            warning=message,
            stop_reason=stop_reason,
        )

    def task_id_for(
        self, agent_name: AgentIdentity, round_number: int, attempt: int = 1
    ) -> str:
        direct = self.task_ids.get((agent_name, round_number, attempt))
        if direct is not None:
            return direct
        round_task = self.task_ids.get((agent_name, round_number))
        if round_task is not None:
            return round_task
        default_task = self.task_ids.get(agent_name)
        if default_task is None:
            raise KeyError(f"missing task for agent {agent_name}")
        return default_task

    def evidence_ids_for(
        self, agent_name: AgentIdentity, round_number: int, attempt: int = 1
    ) -> set[str]:
        task_id = self.task_id_for(agent_name, round_number, attempt)
        return {
            evidence_id
            for call in self.tool_calls
            if call.task_id == task_id
            for evidence_id in call.output_evidence_ids
        }

    def identity_for_task_id(
        self, task_id: str
    ) -> tuple[AgentIdentity, int, int] | None:
        for identity, candidate in self.task_ids.items():
            if candidate != task_id or not isinstance(identity, tuple):
                continue
            if len(identity) == 3:
                return identity
            return identity[0], identity[1], 1
        return None

    def _validate_scope(
        self, query: QueryWindow | ScopedTelemetryQuery | MemoryQuery
    ) -> None:
        window = _query_window(query)
        if window is not None:
            incident_span = timedelta(minutes=self.event.time_window_minutes)
            incident_start = self.event.started_at - incident_span
            incident_end = self.event.started_at + incident_span
            if window[1] < incident_start or window[0] > incident_end:
                raise ValueError("query window does not intersect incident window")
        if isinstance(query, DependencyQuery):
            if query.target and query.target not in self._allowed_targets:
                raise ValueError("dependency target outside investigation scope")

    def _action_timeout(self) -> float:
        remaining = max(0.0, float(self._remaining_deadline_seconds()))
        if remaining <= 0:
            raise ActionDeadlineExceeded("tool deadline exhausted")
        return min(self.tool_timeout_seconds, remaining)

    def _reject(
        self,
        agent_name: AgentIdentity,
        tool_name: str,
        input_value: dict[str, Any],
        status: ToolCallStatus,
        message: str,
        stop_reason: AdaptiveStopReason | None = None,
        *,
        task_id: str,
    ) -> str:
        now = datetime.now(UTC)
        safe_input = redact_value(input_value)
        call = ToolCallRecord(
            task_id=task_id,
            agent_name=_agent_value(agent_name),
            tool_name=tool_name,
            input=safe_input if isinstance(safe_input, dict) else {},
            status=status,
            error_message=message,
            started_at=now,
            completed_at=now,
            runtime_run_id=self.runtime_run_id,
        )
        self.tool_calls.append(call)
        if stop_reason is not None:
            self._stop(agent_name, stop_reason)
        return _response(status=status, evidence=[], warning=message, stop_reason=stop_reason)

    def _stop(self, agent_name: AgentIdentity, reason: AdaptiveStopReason) -> None:
        self._stopped_agents.add(agent_name)
        self.stop_reasons[agent_name] = reason

    @staticmethod
    def _parse_input(raw_input: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw_input)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def project_tool_evidence(evidence: list[EvidenceItem]) -> list[dict[str, JsonValue]]:
    """仅向模型暴露定位 Evidence 所需的稳定字段。"""
    return [
        {
            "id": item.id,
            "provider": item.provider.value,
            "kind": item.kind.value,
            "status": item.status.value,
            "timestamp": item.timestamp.isoformat(),
            "summary": item.summary,
        }
        for item in evidence
    ]


def _query_window(
    query: QueryWindow | ScopedTelemetryQuery | MemoryQuery,
) -> tuple[datetime, datetime] | None:
    if isinstance(query, QueryWindow):
        return query.start_time, query.end_time
    if isinstance(query, ScopedTelemetryQuery):
        if query.window_start is None or query.window_end is None:
            return None
        return query.window_start, query.window_end
    return None


def _query_fingerprint(
    tool_name: str, query: QueryWindow | ScopedTelemetryQuery | MemoryQuery
) -> str:
    values = query.model_dump(mode="json", exclude={"reason"})
    window = _query_window(query)
    if window is not None:
        if isinstance(query, QueryWindow):
            values["start_time"] = window[0].astimezone(UTC).isoformat()
            values["end_time"] = window[1].astimezone(UTC).isoformat()
        else:
            values["window_start"] = window[0].astimezone(UTC).isoformat()
            values["window_end"] = window[1].astimezone(UTC).isoformat()
    for name in ("keywords", "levels", "metric_names"):
        items = values.get(name)
        if not isinstance(items, list):
            continue
        normalized = [str(item) for item in items]
        if name in {"keywords", "levels"}:
            normalized = [item.casefold() for item in normalized]
        values[name] = sorted(set(normalized))
    canonical = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def tool_idempotency_key(
    *,
    run_id: str | None,
    agent_name: AgentIdentity,
    logical_step: str,
    tool_name: str,
    normalized_input: dict[str, Any],
) -> str:
    """从安全规范化输入生成稳定 key，不包含 reason、凭证或证据正文。"""
    canonical = json.dumps(
        normalized_input,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = ":".join(
        (run_id or "compat", _agent_value(agent_name), logical_step, tool_name, canonical)
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _agent_value(agent_name: AgentIdentity) -> str:
    return getattr(agent_name, "value", str(agent_name))


def _result_warning(results: list[ProviderResult]) -> str | None:
    messages = [
        result.error_message or f"{result.provider.value} provider {result.status.value}"
        for result in results
        if result.status in {ProviderStatus.PARTIAL, ProviderStatus.FAILED, ProviderStatus.SKIPPED}
    ]
    return "; ".join(messages) or None


def _response(
    *,
    status: ToolCallStatus,
    evidence: list[dict[str, JsonValue]],
    warning: str | None,
    stop_reason: AdaptiveStopReason | None,
) -> str:
    return json.dumps(
        {
            "status": status.value,
            "evidence": evidence,
            "warning": warning,
            "stop_reason": stop_reason.value if stop_reason else None,
        },
        ensure_ascii=False,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
