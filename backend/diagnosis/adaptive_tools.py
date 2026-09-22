from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from agents import FunctionTool
from agents.exceptions import ModelBehaviorError
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ValidationError

from backend.diagnosis.evidence_comparison import share_trace_semantics
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceProvider, JsonValue
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
from backend.safety.redaction import redact_text, redact_value
from backend.tools.provider_tools import QUERY_MODELS_BY_TOOL
from backend.tools.registry import ToolInvocationResult, ToolRegistry

_COMPACT_SCHEMA_KEYS = (
    "type",
    "enum",
    "const",
    "required",
    "additionalProperties",
    "format",
)
_SERVER_QUERY_FIELDS = frozenset(
    {"start_time", "end_time", "window_start", "window_end", "reason", "limit"}
)


def _compact_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """压缩模型可见 schema，保留类型/必填/枚举语义。

    工具调用进入服务端后仍由完整的 Pydantic query model 校验；这里仅移除
    title、description、长度和正则等重复元数据，避免低预算请求在发送前被
    工具 schema 消耗。read-only、工具名单和服务端校验不因此改变。
    """
    definitions = schema.get("$defs", {})

    def visit(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str):
            name = reference.rsplit("/", 1)[-1]
            if name in stack:
                return {"type": "object"}
            return visit(definitions.get(name, {}), (*stack, name))

        compact = {
            key: node[key] for key in _COMPACT_SCHEMA_KEYS if key in node
        }
        if "properties" in node:
            compact["properties"] = {
                key: visit(value, stack)
                for key, value in node["properties"].items()
            }
        if "items" in node:
            compact["items"] = visit(node["items"], stack)
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                compact[key] = [visit(value, stack) for value in node[key]]
        return compact

    return visit(schema)


def _model_query_schema(tool_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """移除由服务端上下文确定的查询字段和默认值。"""
    compact = _compact_json_schema(schema)
    query_model = QUERY_MODELS_BY_TOOL.get(tool_name)
    if query_model is None or compact.get("type") != "object":
        return compact
    properties = compact.get("properties")
    if not isinstance(properties, dict):
        return compact
    for field_name in _SERVER_QUERY_FIELDS:
        properties.pop(field_name, None)
    required = [
        field_name
        for field_name in compact.get("required", [])
        if field_name in properties
        and field_name in query_model.model_fields
        and query_model.model_fields[field_name].is_required()
    ]
    if required:
        compact["required"] = required
    else:
        compact.pop("required", None)
    return compact


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

    def __init__(self, category: FailureCategory, *, audit_code: str | None = None) -> None:
        self.category = category
        # 只允许调用方传入固定、非模型内容的结构摘要，便于持久化失败审计。
        self.audit_code = audit_code
        super().__init__(category.value)


def retryable_failure_category(exc: BaseException) -> FailureCategory | None:
    """只把明确的 transport/rate-limit/畸形输出异常交给统一 retry coordinator。

    裸 TimeoutError 不在此处放行：工具路径的超时契约是不重试（有测试围栏）；
    模型调用的超时由 v11_runtime 在持久化后包装为 ClassifiedRetryableError
    再进入本函数。
    """
    if isinstance(exc, RateLimitError):
        return FailureCategory.RATE_LIMIT
    if isinstance(exc, APITimeoutError):
        return FailureCategory.TIMEOUT
    if isinstance(exc, APIConnectionError):
        return FailureCategory.TRANSPORT
    if isinstance(exc, APIStatusError) and getattr(exc, "status_code", 0) >= 500:
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
        remaining_tool_budget: Callable[[], int] | None = None,
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
        self._remaining_tool_budget = remaining_tool_budget
        self._fingerprints: set[str] = set()
        self._known_evidence_ids = {item.id for item in seed_evidence}
        self._evidence_by_id = {
            item.id: item for item in seed_evidence
            if item.runtime_run_id == self.runtime_run_id
        }
        self._stopped_agents: set[AgentIdentity] = set()
        self._allowed_targets = {event.service, *(allowed_targets or set())}
        self._extend_allowed_targets(seed_evidence)

    @property
    def remaining_tool_calls(self) -> int:
        """当前会话的剩余尝试数，同时受运行及阶段的取证额度约束。"""
        remaining = min(
            self.max_total_tool_calls - self._total_attempts,
            self.max_tool_calls_per_specialist - max(self._attempts.values(), default=0),
        )
        if self._remaining_tool_budget is not None:
            remaining = min(remaining, self._remaining_tool_budget())
        return max(0, remaining)

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
            if not self.registry.is_available(tool_name, self.event):
                continue
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
                    params_json_schema=_model_query_schema(
                        spec.name, spec.input_schema
                    ),
                    on_invoke_tool=invoke,
                    strict_json_schema=True,
                    # Provider 自身有超时；SDK 外层计时会把排队与持久化也算进去，
                    # 导致已提交的证据无法返回。整轮 deadline 和外部取消仍然生效。
                    timeout_seconds=None,
                    timeout_behavior="error_as_result",
                )
            )
        return tools

    async def invoke(
        self, agent_name: AgentIdentity, tool_name: str, raw_input: str,
        round_number: int, attempt: int = 1,
    ) -> str:
        known = set(self._known_evidence_ids)
        response = json.loads(await self._invoke(
            agent_name, tool_name, raw_input, round_number, attempt,
        ))
        response["remaining_tool_calls"] = self.remaining_tool_calls
        response["new_evidence_count"] = sum(
            item.get("id") not in known for item in response.get("evidence", [])
        )
        return _bounded_tool_response(response)

    async def _invoke(
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

        if not self.registry.is_available(tool_name, self.event):
            return self._reject(
                agent_name, tool_name, {}, ToolCallStatus.SKIPPED,
                "tool provider not configured", task_id=task_id,
            )

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
            query_model = QUERY_MODELS_BY_TOOL[tool_name]
            query = query_model.model_validate(
                _server_query_defaults(self.event, query_model, parsed)
            )
        except ValidationError as exc:
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                _format_query_validation_error(exc, query_model),
                task_id=task_id,
            )
        except (KeyError, TypeError):
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                "invalid tool input",
                task_id=task_id,
            )
        try:
            self._validate_scope(query)
        except ValueError:
            warning = "tool input outside investigation scope"
            if isinstance(query, DependencyQuery) and query.target:
                scoped_targets = self._scoped_target_feedback()
                if scoped_targets:
                    warning += f"; scoped targets: {scoped_targets}"
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                warning,
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
            evidence = [
                self._evidence_by_id[item_id]
                for item_id in reused.output_evidence_ids
                if item_id in self._evidence_by_id
            ]
            warning = reused.error_message
            if len(evidence) != len(reused.output_evidence_ids):
                warning = "reused result contains evidence outside current context scope"
            return _response(
                status=ToolCallStatus.SUCCESS,
                evidence=project_tool_evidence(evidence),
                warning=warning,
                stop_reason=None,
                truncated="query_result_truncated:" in (reused.error_message or ""),
            )
        if fingerprint in self._fingerprints:
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "duplicate query; choose different filters or another tool",
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
                budget_charged=True,
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
            return self._reject(
                agent_name, tool_name, safe_normalized_input, ToolCallStatus.SKIPPED,
                "adaptive tool budget exhausted", AdaptiveStopReason.BUDGET_EXHAUSTED,
                task_id=task_id,
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
                "budget_charged": True,
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
            self._extend_allowed_targets([item])
            if item.runtime_run_id == self.runtime_run_id:
                self._evidence_by_id[item.id] = item
            if item.id in self._known_evidence_ids:
                continue
            self._known_evidence_ids.add(item.id)
            self.new_evidence.append(item)

        truncated = any(item.truncated for item in result.provider_results)
        warning = _result_warning(result.provider_results)
        # 空结果/已有证据只说明本次查询没有增量，不能终止其它工具的取证。
        # 相同查询仍去重，后续查询仍受总预算、单路预算和截止时间约束。
        if not new_output_ids and not truncated and result.call.status != ToolCallStatus.FAILED:
            guidance = (
                "no matching records; coverage is unverified, not evidence of health. "
                "Check entity/time coverage once before interpreting absence; otherwise "
                "record the gap instead of repeating keyword variants"
                if not output_ids else "only previously seen evidence; query again only "
                "with a discriminator that could change the assessment"
            )
            if not output_ids and tool_name == "read_logs":
                guidance += "; keywords use AND within one record, not OR"
            if not output_ids and tool_name == "query_traces":
                guidance += (
                    "; service/entity_ids select span owners. Duration and error filters "
                    "can exclude fast successful server spans on a slow or failing RPC. "
                    "Query the caller and operation for peer timing, or remove these "
                    "filters once to check server coverage; do not infer absent traces"
                )
            warning = "; ".join(filter(None, [
                warning, f"no new evidence; {guidance}",
            ]))

        return _response(
            status=result.call.status,
            evidence=project_tool_evidence(result.evidence),
            warning=warning,
            stop_reason=None,
            truncated=truncated,
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

    def retrieved_evidence(self) -> list[EvidenceItem]:
        """返回本会话实际读到的证据，包括摘要省略但查询命中的已有证据。"""
        ids = dict.fromkeys(
            item_id for call in self.tool_calls for item_id in call.output_evidence_ids
        )
        return [self._evidence_by_id[item_id] for item_id in ids
                if item_id in self._evidence_by_id]

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
            if query.target and not self._target_is_allowed(query.target):
                raise ValueError("dependency target outside investigation scope")

    def _extend_allowed_targets(self, evidence: list[EvidenceItem]) -> None:
        """把已验证证据暴露的实体加入当前 investigation 的 scope。"""
        for item in evidence:
            payload = item.payload
            targets = {
                value
                for value in (item.scope.entity_ids if item.scope is not None else [])
                if isinstance(value, str)
            }
            if isinstance(payload, dict):
                for key in ("service", "entity", "entity_id", "target"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        targets.add(value)
                dependencies = payload.get("dependencies", [])
                if isinstance(dependencies, list):
                    targets.update(
                        value for value in dependencies if isinstance(value, str)
                    )
                edges = payload.get("edges", [])
                if isinstance(edges, list):
                    targets.update(
                        value
                        for edge in edges
                        if isinstance(edge, dict)
                        for value in (edge.get("parent"), edge.get("child"))
                        if isinstance(value, str)
                    )
                if payload.get("runtime_kind") == "pod":
                    for value in tuple(targets):
                        parts = value.split("-")
                        if len(parts) >= 3:
                            # Pod 名通常为 service-replicaset-pod；只把该
                            # provider 已声明为 Pod 的实体折叠回 service 别名。
                            targets.add("-".join(parts[:-2]))
            self._allowed_targets.update(targets)

    def _target_is_allowed(self, target: str) -> bool:
        if target in self._allowed_targets:
            return True
        # 兼容尚未携带 runtime_kind 的历史 Pod 证据，但仍要求完整的
        # scoped entity 以 target- 开头，避免把任意服务名放宽为通配符。
        return any(
            known.startswith(f"{target}-") for known in self._allowed_targets
        )

    def _scoped_target_feedback(self) -> str:
        return ", ".join(
            redact_text(target) for target in sorted(self._allowed_targets)[:20]
        )

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
            budget_charged=False,
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
            "scope_entity_ids": sorted(item.scope.entity_ids) if item.scope else [],
            **project_metric_details(item),
            **project_trace_details(item),
            **project_log_details(item),
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


def _server_query_defaults(
    event: IncidentEvent,
    query_model: type[BaseModel],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    """为模型省略的公共查询字段注入当前 incident 的安全默认值。"""
    if not issubclass(query_model, (QueryWindow, ScopedTelemetryQuery)):
        return parsed
    span = timedelta(minutes=event.time_window_minutes)
    defaults = {"limit": 20}
    if issubclass(query_model, QueryWindow):
        defaults.update(
            {
                "start_time": event.started_at - span,
                "end_time": event.started_at + span,
                "reason": "bounded read-only incident query",
            }
        )
    else:
        defaults.update(
            {
                "window_start": event.started_at - span,
                "window_end": event.started_at + span,
            }
        )
    return {**defaults, **parsed}


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


def _format_query_validation_error(
    error: ValidationError, query_model: type[BaseModel]
) -> str:
    """向模型返回可修正的字段错误，但不回显未经脱敏的输入值。"""
    details: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "input"
        message = redact_text(str(item.get("msg", "invalid value")))
        details.append(f"{location}: {message}")
    if not details:
        return "invalid tool input"
    fields = ", ".join(query_model.model_fields)
    return (
        "invalid tool input: "
        + "; ".join(details[:6])
        + f"; valid fields: {fields}"
    )


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
    truncated: bool = False,
) -> str:
    return _bounded_tool_response(
        {
            "status": status.value,
            "evidence": evidence,
            "returned_count": len(evidence),
            "truncated": truncated,
            "total_count": None,
            "warning": warning,
            "stop_reason": stop_reason.value if stop_reason else None,
        }
    )


def order_trace_evidence(evidence: list[dict]) -> list[dict]:
    """优先保留错误、配对及子调用事实，并轮转操作类型；不推断故障原因。"""
    groups: dict[tuple, list[dict]] = {}

    def priority(item):
        details = item.get("trace_details")
        details = details if isinstance(details, dict) else {}
        timing = item.get("child_timing")
        timing = timing if isinstance(timing, dict) else {}
        duration = item.get("duration_ms", 0)
        duration = duration if type(duration) in {int, float} and math.isfinite(duration) else 0
        return ("query_population" in item, item.get("span_status") == "error",
                "longest_child_peer_duration_ms" in timing,
                details.get("rpc.peer_role") == "callee",
                bool(timing), bool(details.get("rpc.peer_span_id")),
                duration, str(item.get("timestamp", item.get("observed_at", ""))),
                str(item.get("id", "")))

    for item in evidence:
        if item.get("provider") != "trace":
            continue
        details = item.get("trace_details")
        details = details if isinstance(details, dict) else {}
        key = tuple(value if isinstance(value, str) else None for value in (
            item.get("service"), item.get("operation"), item.get("span_status"),
            details.get("rpc.peer_role"),
        ))
        groups.setdefault(key, []).append(item)
    queues = [sorted(items, key=priority, reverse=True) for items in groups.values()]
    queues.sort(key=lambda items: priority(items[0]), reverse=True)
    # 同一操作及状态保留耗时两端，不能让一串最慢样本遮蔽成功调用的差异。
    for index, items in enumerate(queues):
        contrasted = []
        left, right = 0, len(items) - 1
        while left <= right:
            contrasted.append(items[left])
            if left != right:
                contrasted.append(items[right])
            left, right = left + 1, right - 1
        queues[index] = contrasted
    error_operations = {(key[0], key[1]) for key in groups if key[2] == "error"}
    for items in queues:
        head = items[0]
        operation_key = tuple(value if isinstance(value, str) else None
                              for value in (head.get("service"), head.get("operation")))
        if (head.get("span_status") == "ok"
                and operation_key in error_operations):
            valid = [item for item in items if type(item.get("duration_ms")) in {int, float}
                     and math.isfinite(item["duration_ms"]) and item["duration_ms"] >= 0]
            if valid:
                fastest = min(valid, key=lambda item: (item["duration_ms"], str(item.get("id"))))
                items.remove(fastest)
                items.insert(0, fastest)
    ordered = [items[offset] for offset in range(max(map(len, queues), default=0))
               for items in queues if offset < len(items)]
    iterator = iter(ordered)
    return [next(iterator) if item.get("provider") == "trace" else item for item in evidence]


def _bounded_tool_response(payload: dict, max_chars: int = 6000) -> str:
    """只缩小模型可见页；完整 Evidence 已入库，省略项需通过收窄查询获取。"""
    payload = {**payload, **share_trace_semantics(payload["evidence"])}
    original_count = len(payload["evidence"])

    def encode() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    rendered = encode()
    if len(rendered) > max_chars:
        # 先压缩重复结构及可重取的时间桶，再省略整条证据；否则一次新增查询
        # 就可能把前一次查询中用于区分机制的资源组成全部挤掉。
        compacted = []
        for original in order_trace_evidence(payload["evidence"]):
            item = dict(original)
            if (item.get("provider") == "trace" and "duration_ms" in item
                    and "span_status" in item):
                # 数值与错误状态已有独立字段，删去重复文本以保住子 RPC 和配对关系。
                item.pop("summary", None)
                if isinstance(item.get("rpc_pair"), dict):
                    details = item.get("trace_details")
                    if isinstance(details, dict):
                        item["trace_details"] = {key: value for key, value in details.items()
                                                 if key in {"rpc.system", "rpc.status_code",
                                                            "rpc.peer_span_id"}}
                timing = item.get("child_timing")
                if isinstance(timing, dict):
                    item["child_timing"] = {key: value for key, value in timing.items()
                                            if key != "semantics"}
            if item.get("metric") and "baseline_mean" in item and "observation_mean" in item:
                removable = ["time_profile", "related_metric_names"]
                if item.get("scope_entity_ids"):
                    removable.append("summary")
                omitted = [key for key in removable
                           if key in item]
                for key in omitted:
                    item.pop(key)
                observations = item.get("related_observations")
                if (isinstance(observations, list) and observations
                        and "related_observation_columns" not in item):
                    columns = ["metric", "baseline_mean", "observation_mean",
                               "first_sustained_deviation"]
                    item["related_observation_columns"] = columns
                    item["related_observations"] = [
                        [point.get(key) for key in columns] for point in observations
                        if isinstance(point, dict)
                    ]
                if omitted:
                    item["omitted_detail_fields"] = omitted
            compacted.append(item)
        payload["evidence"] = compacted
        rendered = encode()
    while len(rendered) > max_chars and payload["evidence"]:
        payload["evidence"].pop()
        payload.update(
            returned_count=len(payload["evidence"]),
            truncated=True,
            omitted_count=payload.get("omitted_count", 0) + 1,
            page_count=payload.get("page_count", original_count),
            retrieval_hint="Narrow entity, time or signal filters to retrieve omitted evidence.",
        )
        rendered = encode()
    return rendered


def compact_tool_history(value):
    """限制累计工具结果；保留调用/返回配对、状态和省略提示，不改 SDK 原历史。"""
    if not isinstance(value, list):
        return value
    results = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        try:
            payload = json.loads(item.get("output", ""))
        except (ValueError, TypeError):
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("evidence"), list)
            and {"status", "returned_count", "truncated", "stop_reason"} <= payload.keys()
        ):
            results[index] = payload
    if not results or sum(len(value[index]["output"]) for index in results) <= 6000:
        return value
    # 空查询不能占据和满页相同的份额；先满足短结果，再均分剩余空间。
    remaining = 6000
    allocations = {}
    indices = sorted(results, key=lambda index: (len(value[index]["output"]), index))
    for offset, index in enumerate(indices):
        allowance = min(len(value[index]["output"]), remaining // (len(indices) - offset))
        allocations[index] = allowance
        remaining -= allowance
    return [
        {**item, "output": _bounded_tool_response(results[index], allocations[index])}
        if index in results else item
        for index, item in enumerate(value)
    ]


def project_trace_details(item: EvidenceItem) -> dict[str, JsonValue]:
    """保留调用链关联和配对事实，避免仅凭服务端耗时宣称调用链健康。"""
    if item.provider != EvidenceProvider.TRACE:
        return {}
    projected = {key: value[:128] if isinstance(value, str) else value
                 for key in ("trace_id", "span_id", "parent_span_id", "service", "operation")
                 if isinstance(value := item.payload.get(key), str) or value is None}
    population = item.payload.get("query_population")
    if isinstance(population, dict):
        # 原始记录已过 JSON/脱敏校验；仍限制模型可见字段和行数。
        projected["query_population"] = {
            key: value[:512] if isinstance(value, str) else value
            for key in ("service", "operation", "scan_complete", "matching_count",
                        "matched_start", "matched_end", "filters", "columns", "semantics")
            if (value := population.get(key)) is not None
        }
        rows = population.get("rows")
        if isinstance(rows, list):
            projected["query_population"]["rows"] = [
                row for row in rows[:6] if isinstance(row, list) and len(row) == 8
            ]
    attributes = item.payload.get("attributes")
    duration = item.payload.get("duration_ms")
    if type(duration) in {int, float} and math.isfinite(duration) and duration >= 0:
        projected["duration_ms"] = duration
    span_status = item.payload.get("status")
    if isinstance(span_status, str) and span_status in {"ok", "error", "unset"}:
        projected["span_status"] = span_status
    timing = item.payload.get("child_timing")
    if isinstance(timing, dict):
        projected["child_timing"] = {
            key: value for key in ("observed_child_count", "covered_ms", "uncovered_ms",
                                   "longest_child_duration_ms", "longest_child_peer_duration_ms")
            if type(value := timing.get(key)) in {int, float}
            and math.isfinite(value) and value >= 0
        }
        projected["child_timing"].update({
            key: value[:128] for key in ("longest_child_span_id", "longest_child_operation",
                                        "longest_child_peer_service")
            if isinstance(value := timing.get(key), str)
        })
        projected["child_timing"]["semantics"] = (
            "observed same-service direct children, including outbound RPC waits; "
            "uncovered time is NOT CPU self-time; instrumentation may be incomplete. "
            "Child wait does not exclude caller scheduling, reclaim or IO stalls"
        )
    if isinstance(attributes, dict):
        role = attributes.get("rpc.peer_role")
        try:
            peer_ms = float(attributes.get("rpc.peer_duration_ms", "nan"))
        except (TypeError, ValueError, OverflowError):
            peer_ms = float("nan")
        if (isinstance(role, str) and role in {"caller", "callee"}
                and "duration_ms" in projected
                and math.isfinite(peer_ms) and peer_ms >= 0):
            local_client = role == "callee"
            projected["rpc_pair"] = {
                "client_service": item.payload.get("service") if local_client
                                  else attributes.get("rpc.peer_service"),
                "server_service": attributes.get("rpc.peer_service") if local_client
                                  else item.payload.get("service"),
                "client_duration_ms": duration if local_client else peer_ms,
                "server_duration_ms": peer_ms if local_client else duration,
                "semantics": "Client-server difference includes transport, proxy, queueing "
                "and uninstrumented caller work. It does not distinguish network faults "
                "from caller resource stalls; compare same-window caller resource evidence.",
            }
        projected["trace_details"] = {
            key: value[:256] for key in (
                "rpc.system", "rpc.status_code", "rpc.peer_span_id", "rpc.peer_service",
                "rpc.peer_role", "rpc.peer_duration_ms", "rpc.duration_difference_ms",
                "source_service",
            ) if isinstance(value := attributes.get(key), str)
        }
    return redact_value(projected)


def project_log_details(item: EvidenceItem) -> dict[str, JsonValue]:
    """错误说明与消息分离呈现；统一脱敏后限长，避免截断破坏脱敏模式。"""
    if item.provider != EvidenceProvider.LOG:
        return {}
    projected = {key: redact_text(value)[:500] for key in ("error", "level")
                 if isinstance(value := item.payload.get(key), str) and value}
    sampling = item.payload.get("sampling")
    if isinstance(sampling, dict):
        projected["sampling"] = {
            key: redact_text(value)[:128] if isinstance(value, str) else value
            for key in ("selection", "matched_start", "matched_end", "matching_count",
                        "scan_complete")
            if type(value := sampling.get(key)) in {str, bool, int}
        }
    return projected


def project_metric_details(item: EvidenceItem, *, compact: bool = False) -> dict[str, JsonValue]:
    """保留有界判别观测；角色摘要省略时间桶细项，完整数据仍可审计和查询。"""
    projected = {}
    for key in ("window_start", "window_end", "split_at", "timestamp_semantics"):
        value = item.payload.get(key)
        if isinstance(value, str):
            projected[key] = value[:128]
    deviation = item.payload.get("first_sustained_deviation")
    if isinstance(deviation, dict) and isinstance(deviation.get("timestamp"), str):
        projected["first_sustained_deviation"] = {
            "timestamp": deviation["timestamp"][:40],
            "rule": "3 consecutive samples beyond max(3 MAD, 10% baseline median); "
                    "descriptive threshold, not causal onset",
        }
    numeric_keys = ("baseline_mean", "observation_mean", "baseline_min", "baseline_max",
                    "observation_min", "observation_max", "sample_count",
                    "sampling_interval_seconds", "max_sample_gap_seconds")
    for key in numeric_keys:
        value = item.payload.get(key)
        if type(value) in {int, float} and math.isfinite(value):
            projected[key] = value
    for key in ("aggregation", "unit", "value_semantics") + (() if compact else ("metric",)):
        value = item.payload.get(key)
        if isinstance(value, str):
            projected[key] = value[:256]
    names = item.payload.get("related_metric_names")
    if isinstance(names, list) and names:
        projected["related_metric_names"] = [name[:256] for name in names[:8]
                                            if isinstance(name, str)]
    observations = item.payload.get("related_observations")
    if isinstance(observations, list):
        valid = [point for point in observations[:8] if isinstance(point, dict)
                 and isinstance(point.get("metric"), str)
                 and all(type(point.get(k)) in {int, float} and math.isfinite(point[k])
                         for k in ("baseline_mean", "observation_mean"))]
        if valid:
            projected["related_observations"] = [
                {"metric": point["metric"][:256],
                 "baseline_mean": float(f"{point['baseline_mean']:.6g}"),
                 "observation_mean": float(f"{point['observation_mean']:.6g}"),
                 **({"first_sustained_deviation": point["first_sustained_deviation"][:40]}
                    if isinstance(point.get("first_sustained_deviation"), str) else {})}
                for point in valid
            ]
            # 名称已包含在观测表里，避免重复长指标名。
            projected.pop("related_metric_names", None)
    profile = item.payload.get("time_profile")
    if isinstance(profile, list):
        projected["time_profile"] = [
            {"start": point["start"][:40], "end": point["end"][:40],
             "mean": float(f"{point['mean']:.6g}")}
            for point in profile[:8]
            if isinstance(point, dict)
            and isinstance(point.get("start"), str) and isinstance(point.get("end"), str)
            and type(point.get("mean")) in {int, float} and math.isfinite(point["mean"])
        ]
    return redact_value(projected)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
