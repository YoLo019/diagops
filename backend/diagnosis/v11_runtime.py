"""V11 Agent authority runtime 的共享边界。

本模块只负责 V11 的 Lead、通用 Investigator、Critic 和最终校验；旧的
``AgentsRcaRuntime`` 仍然只服务 v10_legacy。所有 Provider 调用继续经过
既有 ToolRegistry、幂等记录和 RuntimeWriter 回调。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from agents import (
    Agent,
    AgentOutputSchema,
    FunctionTool,
    Model,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
)
from agents.models.interface import ModelProvider as AgentsModelProvider
from agents.models.multi_provider import MultiProvider
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.db.models import InvestigationStatus
from backend.diagnosis.adaptive_tools import (
    AdaptiveToolSession,
    ClassifiedRetryableError,
    RetryCoordinator,
    retryable_failure_category,
)
from backend.diagnosis.agents_runtime import _run_with_model_lifecycle
from backend.diagnosis.diagnostic_skills import (
    DIAGNOSTIC_SKILLS,
    SKILL_CATALOG_VERSION,
    DiagnosticSkill,
    skill_catalog_hash,
    validate_skill_catalog,
)
from backend.diagnosis.openai_compatible_model import (
    STRICT_TOOL_ENVELOPE_SCHEMA,
    OpenAICompatibleChatCompletionsModel,
    strict_transport_tools,
)
from backend.diagnosis.openai_model import OFFICIAL_OPENAI_BASE_URL
from backend.diagnosis.result_validation import (
    V11ResultValidationError,
    _validate_scope_consistency,
    validate_v11_result,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingSeverity,
    AgentFindingType,
    CausalCheck,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadDecision,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceStatus
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
)
from backend.domain.runtime import (
    V11_RUN_DEADLINE_MAX_SECONDS,
    RuntimePhase,
    validate_v11_execution_contract,
)
from backend.runtime.concurrency import RunStepGate
from backend.safety.redaction import redact_value, safe_exception_diagnostic
from backend.tools.provider_tools import (
    VerifiedMemoryLookup,
    current_investigation_scope,
)
from backend.tools.registry import ToolRegistry, agent_manifest_hash

logger = logging.getLogger(__name__)


class V11RuntimeContractError(ValueError):
    """表示 Agent 输出违反 V11 的机械合同。"""


class V11RuntimeUnavailable(RuntimeError):
    """表示 V11 没有可用的模型调用入口。"""


class EvidenceScopeDraft(BaseModel):
    """模型可声明的有界证据范围；持久化时转换为普通 JSON map。"""

    model_config = ConfigDict(extra="forbid")

    entity_ids: list[str] = Field(default_factory=list, max_length=20)
    start_time: datetime | None = None
    end_time: datetime | None = None


class LeadTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    analysis_round: int = Field(default=1, ge=1, le=2)
    tool_names: list[str] = Field(default_factory=list, max_length=9)
    strategy: str | None = Field(default=None, max_length=128)
    evidence_scope: EvidenceScopeDraft | None = None
    expected_discriminator: str | None = Field(default=None, max_length=256)
    information_gap: str | None = Field(default=None, max_length=256)


class LeadPlanningTaskDraft(LeadTaskDraft):
    """首轮 planning task；round 由 schema 固定为 1。"""

    analysis_round: Literal[1] = 1


class LeadPlanningOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: LeadDecision
    tasks: list[LeadPlanningTaskDraft] = Field(default_factory=list, max_length=3)


class LeadPlanningCompactTaskDraft(BaseModel):
    """live Lead 的最小 planning draft；运行时字段由服务端补齐。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=96)
    description: str = Field(min_length=1, max_length=256)
    information_gap: str = Field(min_length=1, max_length=160)
    expected_discriminator: str | None = Field(default=None, max_length=160)


class LeadPlanningCompactOutput(BaseModel):
    """live Lead 的最小 planning 输出；不暴露 server-owned task 字段。"""

    model_config = ConfigDict(extra="forbid")

    decision: LeadDecision
    tasks: list[LeadPlanningCompactTaskDraft] = Field(
        default_factory=list, max_length=3
    )


class InvestigatorFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_type: AgentFindingType
    summary: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = Field(default="", max_length=512)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    blocking: bool = False
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=256)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class InvestigatorCandidateDraft(BaseModel):
    """模型可见的最小候选草稿；其余持久化字段由服务端生成。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    affected_entity: str = Field(min_length=1, max_length=128)
    failure_mechanism: str = Field(min_length=1, max_length=256)
    supporting_evidence_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=32
    )
    contradicting_evidence_ids: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(default_factory=list, max_length=32)


class InvestigatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=512)
    findings: list[InvestigatorFindingDraft] = Field(default_factory=list, max_length=8)
    candidates: list[InvestigatorCandidateDraft] = Field(
        default_factory=list, max_length=3
    )


class InvestigatorCandidateOutput(BaseModel):
    """已有完整证据时使用的紧凑候选输出契约。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[InvestigatorCandidateDraft] = Field(
        default_factory=list, max_length=1
    )


class V11SingleControlOutput(BaseModel):
    """Single control 的最小诊断输出；planning/task 由服务端预注册。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[InvestigatorCandidateDraft] = Field(
        default_factory=list, max_length=3
    )


class CriticAssessmentDraft(BaseModel):
    """Critic 草稿；candidate_ref 是服务端提供的候选引用，不是实体主键字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_ref: str = Field(min_length=1, max_length=128)
    verdict: CriticVerdict
    checks: list[CausalCheck] = Field(min_length=7, max_length=7)
    supporting_evidence_ids: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(default_factory=list, max_length=32)
    contradicting_evidence_ids: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(default_factory=list, max_length=32)
    gap: str | None = Field(default=None, max_length=256)
    supplemental_task_ids: list[str] = Field(default_factory=list, max_length=3)
    summary: str = Field(min_length=1, max_length=512)


class CriticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=512)
    # 三个并行 Investigator 各自最多提交三个候选；Critic 必须覆盖合并后的
    # 全部候选，不能让 schema 上限把合法候选截掉。
    assessments: list[CriticAssessmentDraft] = Field(
        default_factory=list, max_length=9
    )
    tasks: list[LeadTaskDraft] = Field(default_factory=list, max_length=3)


class CriticCompactCausalCheck(BaseModel):
    """live Critic 的最小 check draft；summary 由服务端规范化。"""

    model_config = ConfigDict(extra="forbid")

    name: CausalCheckName
    status: CausalCheckStatus
    evidence_ids: list[str] = Field(default_factory=list, max_length=1)
    gap: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_check_contract(self) -> CriticCompactCausalCheck:
        if self.status in {CausalCheckStatus.PASS, CausalCheckStatus.FAIL}:
            if not self.evidence_ids or self.gap is not None:
                raise ValueError("pass and fail checks require evidence without gap")
        elif not self.gap:
            raise ValueError("unknown checks require a named gap")
        return self


class CriticCompactAssessmentDraft(BaseModel):
    """已有完整证据时的无 supplemental task Critic 草稿。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_ref: str = Field(min_length=1, max_length=128)
    verdict: Literal[
        CriticVerdict.ACCEPT,
        CriticVerdict.REJECT,
        CriticVerdict.INCONCLUSIVE,
    ]
    checks: list[CriticCompactCausalCheck] = Field(min_length=7, max_length=7)


class CriticCompactOutput(BaseModel):
    """已有完整证据时使用的有界 Critic 输出。"""

    model_config = ConfigDict(extra="forbid")

    assessments: list[CriticCompactAssessmentDraft] = Field(
        default_factory=list, max_length=3
    )


class LeadAdjudicationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: LeadDecision


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    output: Any
    input_tokens: int = 0
    output_tokens: int = 0
    execution_id: str | None = None


def _strict_output_tool(output_type: type[BaseModel]) -> FunctionTool:
    schema = AgentOutputSchema(output_type)

    async def submit(_context, raw_input: str) -> BaseModel:
        envelope = json.loads(raw_input)
        if set(envelope) != {"payload_json"} or not isinstance(
            envelope["payload_json"], str
        ):
            raise V11RuntimeContractError("structured output envelope is invalid")
        return schema.validate_json(envelope["payload_json"])

    return FunctionTool(
        name="submit_structured_output",
        description=(
            "Submit the final structured result. Encode it as a JSON string in "
            "payload_json. The decoded JSON must match this schema: "
            f"{json.dumps(schema.json_schema(), ensure_ascii=False, sort_keys=True)}"
        ),
        params_json_schema=copy.deepcopy(STRICT_TOOL_ENVELOPE_SCHEMA),
        on_invoke_tool=submit,
        strict_json_schema=True,
    )


@dataclass(slots=True)
class _ModelReservation:
    output_cap: int | None
    input_estimate: int
    reserved_total: int
    estimate_audit: dict[str, Any]
    status: str = "reserved"


@dataclass(frozen=True, slots=True)
class _ModelRequestEvent:
    reservation_id: str
    reservation_status: str | None


@dataclass(slots=True)
class _ModelUsageAccumulator:
    """按 logical model call 去重并累计 request/attempt 的 usage。"""

    attempt_totals: dict[int, list[int]]
    event_keys: set[tuple[str, str, str | None, int]]
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    @staticmethod
    def _token_value(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def record_event(
        self,
        *,
        status: str,
        input_tokens: int,
        output_tokens: int,
        safe_payload: dict[str, Any] | None,
    ) -> tuple[int, int] | None:
        payload = safe_payload or {}
        reservation_id = payload.get("reservation_id")
        attempt = payload.get("attempt")
        if not isinstance(reservation_id, str) or not isinstance(attempt, int):
            return None
        reservation_status = payload.get("reservation_status")
        if reservation_status is not None and not isinstance(
            reservation_status, str
        ):
            reservation_status = None
        if status == "completed":
            actual_input = self._token_value(
                payload["actual_input_tokens"]
                if "actual_input_tokens" in payload
                else input_tokens
            )
            actual_output = self._token_value(output_tokens)
        elif status == "failed" and reservation_status != "deferred":
            actual_input = max(
                self._token_value(input_tokens),
                self._token_value(payload.get("input_estimate")),
            )
            actual_output = 0
        else:
            return None
        key = (reservation_id, status, reservation_status, attempt)
        if key in self.event_keys:
            return None
        self.event_keys.add(key)
        totals = self.attempt_totals.setdefault(attempt, [0, 0])
        totals[0] += actual_input
        totals[1] += actual_output
        if status == "completed":
            self.total_input_tokens += actual_input
            self.total_output_tokens += actual_output
            return actual_input, actual_output
        return 0, 0

    def record_fallback(
        self, attempt: int, input_tokens: int, output_tokens: int
    ) -> tuple[int, int] | None:
        if attempt in self.attempt_totals:
            return None
        actual_input = self._token_value(input_tokens)
        actual_output = self._token_value(output_tokens)
        self.event_keys.add((f"attempt-{attempt}", "fallback", None, attempt))
        self.attempt_totals[attempt] = [actual_input, actual_output]
        self.total_input_tokens += actual_input
        self.total_output_tokens += actual_output
        return actual_input, actual_output

    def has_attempt_usage(self, attempt: int) -> bool:
        return attempt in self.attempt_totals

    def attempt_usage(self, attempt: int) -> tuple[int, int]:
        input_tokens, output_tokens = self.attempt_totals.get(attempt, [0, 0])
        return input_tokens, output_tokens


_INPUT_ESTIMATE_METHOD = "unicode-json-envelope-v2"
_JSON_PUNCTUATION = frozenset('{}[],:"\\')
_INPUT_ESTIMATE_CALIBRATION_SAFETY_NUMERATOR = 5
_INPUT_ESTIMATE_CALIBRATION_SAFETY_DENOMINATOR = 4
_INPUT_ESTIMATE_CALIBRATION_FLOOR_BASIS_POINTS = 5000
_INPUT_ESTIMATE_BASIS_POINTS = 10000


def _is_cjk_character(character: str) -> bool:
    """识别需要按接近逐字计量的 CJK/日韩文字。"""
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x2FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _estimate_text_tokens(text: str) -> tuple[int, dict[str, int]]:
    """计算 provider-neutral 的保守输入 token 包络。

    这里不是某个模型的 tokenizer：兼容 endpoint 可能使用未知 tokenizer，
    只能先按普通 ASCII、JSON 标点和 CJK 分开计量。实际 provider usage 会在
    settle 时继续作为权威值审计；这个包络的职责是避免明显低估后才发现超支。
    """
    ascii_plain = 0
    cjk = 0
    non_ascii = 0
    json_punctuation = 0
    for character in text:
        if _is_cjk_character(character):
            cjk += 1
        elif ord(character) > 0x7F:
            non_ascii += 1
        elif character in _JSON_PUNCTUATION:
            json_punctuation += 1
        else:
            ascii_plain += 1

    # ASCII 文本按 4 字符/token；JSON 标点按约 2 字符/token；CJK 按 1.5
    # token/字向上取整；其他非 ASCII 字符按 2 token 保守处理。兼容端点
    # 的实际 usage 会在结算时覆盖估算，低预算下避免 JSON schema 标点的
    # 逐字符包络吞掉结构化结果所需的最小输出空间。
    estimated = (
        (ascii_plain + 3) // 4
        + (cjk * 3 + 1) // 2
        + non_ascii * 2
        + (json_punctuation + 1) // 2
    )
    return max(1, estimated), {
        "chars": len(text),
        "ascii_plain_chars": ascii_plain,
        "cjk_chars": cjk,
        "non_ascii_chars": non_ascii,
        "json_punctuation_chars": json_punctuation,
    }


def _serialized_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        # 只用于无敏感内容的长度审计和预算包络；模型请求本身仍由 SDK
        # 负责序列化，不能用这个 fallback 伪造请求内容。
        return repr(value)


def _estimate_model_input(
    prompt: str, context: dict[str, Any]
) -> tuple[int, dict[str, int | str]]:
    """估算当前请求并返回不含原文的组成统计。"""
    context_json = _serialized_value(context)
    total_text = f"{prompt}{context_json}"
    estimated, counts = _estimate_text_tokens(total_text)
    audit: dict[str, int | str] = {
        "method": _INPUT_ESTIMATE_METHOD,
        "estimated_tokens": estimated,
        "instruction_chars": len(prompt),
        "context_chars": len(context_json),
        "total_chars": len(total_text),
        **counts,
    }
    for component in ("input", "tools", "output_schema"):
        if component not in context:
            continue
        component_text = _serialized_value(context[component])
        component_estimate, component_counts = _estimate_text_tokens(component_text)
        audit[f"{component}_chars"] = len(component_text)
        audit[f"{component}_estimated_tokens"] = component_estimate
        audit[f"{component}_cjk_chars"] = component_counts["cjk_chars"]
        audit[f"{component}_json_punctuation_chars"] = component_counts[
            "json_punctuation_chars"
        ]
    return estimated, audit


def _input_estimate_audit(audit: dict[str, Any]) -> dict[str, Any]:
    """以单个受限字段记录估算组成，避免撑爆 RuntimeEvent 顶层字段数。"""
    return {"input_estimate_audit": dict(audit)}


class _V11BudgetedModel(Model):
    """把 durable token reservation 下沉到 Agents SDK 的每个 request。"""

    def __init__(
        self,
        delegate: Model,
        runtime: V11Runtime,
        *,
        logical_call_id: str | None = None,
        execution_id: str | None = None,
        attempt_number: int = 1,
        actor: str = "CoordinatorAgent",
    ) -> None:
        self._delegate = delegate
        self._runtime = runtime
        self._logical_call_id = logical_call_id
        self._execution_id = execution_id
        self._attempt_number = attempt_number
        self._actor = actor
        self._request_index = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        model_settings = kwargs["model_settings"]
        tools = kwargs.get("tools", [])
        output_schema = kwargs.get("output_schema")
        if (
            isinstance(self._delegate, OpenAICompatibleChatCompletionsModel)
            and self._delegate.structured_output_transport == "strict_output_tool"
        ):
            tools = strict_transport_tools(tools)
            output_schema = None
        request_index = self._request_index
        self._request_index += 1
        reservation_id = self._runtime._model_request_reservation_id(
            self._logical_call_id,
            request_index,
        )
        reservation = await self._runtime._reserve_model_budget(
            model_settings.max_tokens,
            kwargs.get("system_instructions") or "",
            {
                "input": kwargs.get("input"),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.params_json_schema,
                    }
                    for tool in tools
                    if isinstance(tool, FunctionTool)
                ],
                "output_schema": (
                    output_schema.json_schema()
                    if output_schema is not None
                    else None
                ),
            },
            reservation_id=reservation_id,
            logical_call_id=self._logical_call_id,
            execution_id=self._execution_id,
            attempt=self._attempt_number,
            actor=self._actor,
            request_index=request_index,
        )
        output_cap, input_estimate, reserved_total = reservation
        request_settings = replace(
            model_settings,
            max_tokens=output_cap,
            retry=ModelRetrySettings(max_retries=0),
        )
        request_args = dict(kwargs)
        request_args["model_settings"] = request_settings
        request_started = False
        try:
            self._runtime._check_execution()
            timeout_seconds = self._runtime._model_timeout()
            request_started = True
            response = await asyncio.wait_for(
                self._delegate.get_response(*args, **request_args),
                timeout=timeout_seconds,
            )
            self._runtime._check_execution()
            input_tokens, output_tokens = _usage_values(
                getattr(response, "usage", None)
            )
            # provider 已返回 usage 时以实际 input 结算，估算只作缺失 usage 的回退；
            # 否则保守估算的多余部分会被错误地永久消耗掉 run 预算。
            settlement_input_tokens = (
                input_tokens if input_tokens > 0 else input_estimate
            )
            await self._runtime._settle_model_budget(
                reserved_total,
                settlement_input_tokens + output_tokens,
                reservation_id=reservation_id,
                logical_call_id=self._logical_call_id,
                execution_id=self._execution_id,
                attempt=self._attempt_number,
                actor=self._actor,
                request_index=request_index,
                input_tokens=settlement_input_tokens,
                actual_input_tokens=input_tokens,
                output_tokens=output_tokens,
                reservation_status="completed",
            )
            return response
        except asyncio.CancelledError:
            if reserved_total:
                await self._runtime._settle_model_budget(
                    reserved_total,
                    input_estimate if request_started else 0,
                    reservation_id=reservation_id,
                    logical_call_id=self._logical_call_id,
                    execution_id=self._execution_id,
                    attempt=self._attempt_number,
                    actor=self._actor,
                    request_index=request_index,
                    input_tokens=input_estimate if request_started else 0,
                    reservation_status="released",
                )
            raise
        except Exception as exc:
            if reserved_total:
                await self._runtime._settle_model_budget(
                    reserved_total,
                    input_estimate if request_started else 0,
                    reservation_id=reservation_id,
                    logical_call_id=self._logical_call_id,
                    execution_id=self._execution_id,
                    attempt=self._attempt_number,
                    actor=self._actor,
                    request_index=request_index,
                    input_tokens=input_estimate if request_started else 0,
                    reservation_status=self._runtime._retry_reservation_status(
                        exc,
                        self._attempt_number,
                    ),
                )
            raise

    async def stream_response(self, *args: Any, **kwargs: Any):
        # V11 使用非流式结构化响应；保留 SDK Model 接口以便 provider adapter 正常解析。
        async for event in self._delegate.stream_response(*args, **kwargs):
            yield event

    async def close(self) -> None:
        await self._delegate.close()


class _V11ModelProvider(AgentsModelProvider):
    """为 string model name 注入同一 request budget proxy。"""

    def __init__(
        self,
        runtime: V11Runtime,
        *,
        logical_call_id: str | None = None,
        execution_id: str | None = None,
        attempt_number: int = 1,
        actor: str = "CoordinatorAgent",
    ) -> None:
        self._runtime = runtime
        self._logical_call_id = logical_call_id
        self._execution_id = execution_id
        self._attempt_number = attempt_number
        self._actor = actor
        self._client = AsyncOpenAI(
            base_url=OFFICIAL_OPENAI_BASE_URL,
            max_retries=0,
        )
        try:
            actual_endpoint = canonicalize_endpoint(str(self._client.base_url))
        except (AttributeError, TypeError, ValueError) as exc:
            raise V11RuntimeContractError(
                "V11 official client endpoint is unavailable"
            ) from exc
        if actual_endpoint != OFFICIAL_OPENAI_BASE_URL:
            raise V11RuntimeContractError("V11 official client endpoint mismatch")
        contract = getattr(runtime, "_execution_contract", None)
        if contract is not None and contract.get("endpoint_id") != endpoint_id(
            OFFICIAL_OPENAI_BASE_URL
        ):
            raise V11RuntimeContractError("V11 official endpoint contract mismatch")
        self._delegate = MultiProvider(openai_client=self._client)

    def get_model(self, model_name: str | None) -> Model:
        return _V11BudgetedModel(
            self._delegate.get_model(model_name),
            self._runtime,
            logical_call_id=self._logical_call_id,
            execution_id=self._execution_id,
            attempt_number=self._attempt_number,
            actor=self._actor,
        )

    async def aclose(self) -> None:
        await self._delegate.aclose()
        await self._client.close()


@dataclass(frozen=True, slots=True)
class _InvestigatorResult:
    findings: tuple[AgentFinding, ...]
    candidates: tuple[RootCauseCandidate, ...]
    execution: AgentExecution
    # 草稿级拒绝的持久化审计（模型输出违约 ≠ investigator 失败）。
    audit_executions: tuple[AgentExecution, ...] = ()


def _draft_rejection_code(exc: V11RuntimeContractError) -> str:
    """把 draft 违约映射为固定审计 code（不带任何模型文本）。"""
    if "non-gap finding requires evidence" in str(exc):
        return "finding draft rejected: non_gap_requires_evidence"
    if "finding draft violates finding contract" in str(exc):
        return "finding draft rejected: invalid_finding_contract"
    return "finding draft rejected: uncommitted_evidence"


def _safe_lifecycle_digest(value: Any) -> str:
    """仅返回摘要哈希，禁止把模型原始结果写入审计。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(type(value)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _structured_validation_signature(exc: BaseException) -> str:
    """提取结构校验的安全位置码，不把模型值或原始响应写入审计。"""
    if not isinstance(exc, ValidationError):
        return type(exc).__name__.lower()[:64]
    signatures: list[str] = []
    for error in exc.errors(include_url=False, include_context=False):
        loc_parts: list[str] = []
        for part in error.get("loc", ()):
            if isinstance(part, int):
                loc_parts.append(str(part))
            elif (
                isinstance(part, str)
                and len(part) <= 64
                and all(char.isalnum() or char in "_-" for char in part)
            ):
                loc_parts.append(part)
            else:
                loc_parts.append("field")
        location = ".".join(loc_parts) or "root"
        error_type = str(error.get("type", "validation_error"))[:64]
        signatures.append(f"{location}:{error_type}")
    return ";".join(signatures[:8])[:256] or "validation_error"


def _candidate_lifecycle_trace(
    *,
    raw_output: Any,
    parsed_drafts: Iterable[BaseModel],
    admitted_candidates: Iterable[RootCauseCandidate],
    failure_categories: Iterable[str] = (),
) -> str:
    """构造安全的候选生命周期审计摘要。"""
    drafts = [item.model_dump(mode="json") for item in parsed_drafts]
    admitted = [item.model_dump(mode="json") for item in admitted_candidates]
    payload = {
        "schema_version": "candidate-lifecycle-v1",
        "raw_structured_output": {
            "count": 1,
            "sha256": _safe_lifecycle_digest(raw_output),
        },
        "parsed_drafts": {
            "count": len(drafts),
            "sha256": _safe_lifecycle_digest(drafts),
        },
        "admitted_candidates": {
            "count": len(admitted),
            "sha256": _safe_lifecycle_digest(admitted),
        },
        "failure_categories": sorted(
            {str(item)[:128] for item in failure_categories if item}
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _investigator_failure_audit(exc: BaseException) -> tuple[str, FailureCategory]:
    """把 Investigator 的边界失败收口为可审计的固定码。"""
    audit_code = getattr(exc, "audit_code", None)
    if isinstance(audit_code, str) and audit_code:
        return (
            f"investigator output invalid: {audit_code[:256]}",
            FailureCategory.INVALID_OUTPUT,
        )
    code_by_message = {
        "model token budget exhausted": (
            "model_token_budget_exhausted",
            FailureCategory.QUOTA,
        ),
        "model response exceeded token budget": (
            "model_token_budget_exceeded",
            FailureCategory.QUOTA,
        ),
        "model tool budget exhausted": (
            "model_tool_budget_exhausted",
            FailureCategory.QUOTA,
        ),
        "V11 model turn budget exhausted": (
            "model_turn_budget_exhausted",
            FailureCategory.QUOTA,
        ),
        "invalid V11 model output": (
            "invalid_v11_model_output",
            FailureCategory.INVALID_OUTPUT,
        ),
        "invalid_output": (
            "invalid_v11_model_output",
            FailureCategory.INVALID_OUTPUT,
        ),
    }
    return code_by_message.get(str(exc), ("investigator failed", FailureCategory.UNKNOWN))


def _critic_failure_audit(exc: BaseException) -> tuple[str, FailureCategory]:
    """把 Critic 边界失败映射为不含模型文本的固定审计码。"""
    audit_code = getattr(exc, "audit_code", None)
    if isinstance(audit_code, str) and audit_code:
        return (
            f"critic output invalid: {audit_code[:256]}",
            FailureCategory.INVALID_OUTPUT,
        )
    message = str(exc)
    if "candidate reference" in message:
        return "critic candidate reference rejected", FailureCategory.INVALID_REFERENCE
    if "evidence reference" in message:
        return "critic evidence reference rejected", FailureCategory.INVALID_REFERENCE
    return "critic output invalid", FailureCategory.INVALID_OUTPUT


def _model_retryable_exception(exc: BaseException) -> BaseException:
    """模型调用超时可重试（网关慢/长生成是运营噪声）；工具路径的裸
    TimeoutError 契约不受影响——转换只发生在模型调用的 re-raise 处。"""
    if isinstance(exc, TimeoutError):
        return ClassifiedRetryableError(FailureCategory.TIMEOUT)
    return exc


def _structured_output_retry_feedback(output_type: type[BaseModel]) -> str:
    """为下一次模型尝试提供固定、可安全持久化的 schema 纠正提示。"""
    common = (
        "The previous structured response was rejected. Return a new response "
        "that satisfies the declared JSON schema exactly: output one JSON object, "
        "use the declared enum values, respect every list bound, and add no extra "
        "fields. Do not return markdown or explain the correction."
    )
    if output_type is LeadPlanningCompactOutput:
        return (
            f"{common} Return one to three concise planning tasks. Each task may "
            "contain only id, title, description, information_gap, and optional "
            "expected_discriminator. Do not return tool names, round numbers, "
            "runtime IDs, review fields, or server-owned fields. Do not conclude "
            "during planning."
        )
    if output_type is CriticCompactOutput:
        return (
            f"{common} For every listed candidate, emit exactly one concise "
            "assessment using its candidate_ref exactly as provided; use only "
            "accept, reject, or inconclusive, emit seven named causal checks "
            "with only name, status, evidence_ids, and gap fields, "
            "and cite at most one committed evidence ID per check. A pass or "
            "fail check must cite evidence and must not include gap; an unknown "
            "check must include a short gap. Do not include summaries, top-level "
            "evidence arrays, gap, or supplemental task fields in this bounded "
            "review. Do not return server assessment IDs, candidate_id, or "
            "extra fields."
        )
    if output_type is CriticOutput:
        return (
            f"{common} For every listed candidate, emit exactly one assessment "
            "using its candidate_ref exactly as provided; do not emit id or "
            "candidate_id fields. "
            "with exactly these seven checks: temporal, topology, mechanism, "
            "blast_radius, symptom_vs_cause, counterevidence, alternatives. "
            "Each pass/fail check must cite evidence_ids; each unknown check must "
            "name a gap. A needs_evidence assessment must include a gap and at "
            "least one supplemental task."
        )
    if output_type in {V11SingleControlOutput, InvestigatorCandidateOutput}:
        return (
            f"{common} Return only candidates with affected_entity, "
            "failure_mechanism, supporting_evidence_ids, and optional "
            "contradicting_evidence_ids. Cite only committed usable evidence IDs; "
            "when cited evidence has scope_entity_ids, affected_entity must "
            "match an entity in every cited evidence scope; "
            "do not emit server-owned IDs, ranks, runtime fields, review fields, "
            "or finding references."
        )
    if output_type is InvestigatorOutput:
        return (
            f"{common} Cite only committed usable evidence IDs available to this "
            "investigator. Do not emit candidate IDs, ranks, runtime IDs, review "
            "fields, or finding references. Every candidate must include a "
            "non-empty affected_entity, a "
            "non-empty failure_mechanism, and at least one supporting usable "
            "evidence ID. A non-gap finding must cite at least one usable evidence "
            "ID. If no candidate meets these requirements, return no candidates. "
            "When the evidence supports a service-level failure symptom but not "
            "a confirmed causal root cause, emit a bounded candidate with an "
            "explicitly unresolved observed mechanism and keep the uncertainty "
            "in the finding. Do not emit a candidate without an affected service "
            "and usable evidence."
        )
    return common


TurnCallable = Callable[..., Awaitable[Any]]


class V11Runtime:
    """在持久化 V11 phase 内运行 Agent-authored diagnosis。"""

    def __init__(
        self,
        *,
        model: Any,
        model_provider: ModelProvider = ModelProvider.OPENAI,
        model_name: str | None = None,
        tool_registry: ToolRegistry | None = None,
        turn: TurnCallable | None = None,
        max_turns: int = 8,
        timeout_seconds: float = 120.0,
        max_investigators: int = 3,
        max_rounds: int = 2,
        max_total_tool_calls: int = 8,
        max_tool_calls_per_specialist: int = 3,
        token_budget: int | None = None,
        tool_timeout_seconds: float = 10.0,
        parallel_limit: RunStepGate | None = None,
        skills: tuple[DiagnosticSkill, ...] = DIAGNOSTIC_SKILLS,
    ) -> None:
        if not 1 <= max_investigators <= 3:
            raise ValueError("max_investigators must be between one and three")
        if max_rounds not in {1, 2}:
            raise ValueError("max_rounds must be one or two")
        if max_total_tool_calls < 1:
            raise ValueError("max_total_tool_calls must be positive")
        if max_tool_calls_per_specialist < 1:
            raise ValueError("max_tool_calls_per_specialist must be positive")
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if timeout_seconds < 1 or timeout_seconds > V11_RUN_DEADLINE_MAX_SECONDS:
            raise ValueError(
                "V11 timeout must be between one and "
                f"{int(V11_RUN_DEADLINE_MAX_SECONDS)} seconds"
            )
        self.model = model
        self.model_provider = ModelProvider(model_provider)
        self.model_name = model_name
        self.tool_registry = tool_registry
        self.turn = turn
        self.max_turns = max_turns
        self.timeout_seconds = timeout_seconds
        self.max_investigators = max_investigators
        self.max_rounds = max_rounds
        self.max_total_tool_calls = max_total_tool_calls
        self.max_tool_calls_per_specialist = max_tool_calls_per_specialist
        self.tool_timeout_seconds = tool_timeout_seconds
        self.skills = skills
        self.runtime_run_id: str | None = None
        self._remaining_token_budget = token_budget
        self._token_budget_lock = asyncio.Lock()
        self._remaining_model_turns: int | None = None
        self._reserve_model_turn_callback: Callable[[], Any] | None = None
        self._model_turn_budget_enabled = False
        self._model_reservations: dict[str, _ModelReservation] = {}
        self._settled_model_reservations: set[str] = set()
        self._model_request_output_caps: dict[tuple[str, int], int] = {}
        self._model_request_history: dict[
            tuple[str, int], list[_ModelRequestEvent]
        ] = {}
        self._model_usage_accumulators: dict[str, _ModelUsageAccumulator] = {}
        # 同一 run 内按 agent 角色累计 provider usage；首次请求仍使用保守包络，
        # 后续请求用实测比例校准，避免未知 tokenizer 长期吞掉 output cap。
        self._input_estimate_calibration: dict[str, list[int]] = {}
        self._commit_lock = asyncio.Lock()
        self._execution_contract: dict[str, Any] | None = None
        self._remaining_deadline_seconds: Callable[[], float] = lambda: float("inf")
        self._deadline_at: datetime | None = datetime.now(UTC) + timedelta(
            seconds=timeout_seconds
        )
        self._phase_tool_budget: int | None = None
        self._parallel_limit = parallel_limit or RunStepGate(max_investigators)
        self._check_execution: Callable[[], None] = lambda: None
        self._persist_tool_start = None
        self._persist_tool_result = None
        self._resolve_tool_result = None
        self._persist_agent_event = None
        self._persist_model_event = None
        self._hit_fault: Callable[[str], None] = lambda _point: None
        self._active_sessions: set[AdaptiveToolSession] = set()
        self._failures: list[str] = []
        self._terminal_failure = False
        self._terminal_failure_reason: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._completed_rounds = 0

        if tool_registry is not None:
            validate_skill_catalog(list(skills), tool_registry.list_agent_specs())

    @staticmethod
    def memory_lookup_from_registry(registry: ToolRegistry) -> VerifiedMemoryLookup:
        """读取宿主注入的 memory resolver，避免 runtime 自建第二份 Provider。"""
        lookup = getattr(registry, "verified_memory_lookup", None)
        if not isinstance(lookup, VerifiedMemoryLookup):
            raise V11RuntimeContractError("verified memory lookup is not wired")
        return lookup

    @property
    def active_session_count(self) -> int:
        """返回仍持有工具会话的数量，供 cleanup 断言使用。"""
        return len(self._active_sessions)

    def clone_for_run(
        self,
        *,
        runtime_run_id: str,
        model_provider: ModelProvider | None,
        model_name: str | None,
        token_budget: int | None,
        timeout_seconds: float | None = None,
        execution_contract: dict[str, Any] | None = None,
    ) -> V11Runtime:
        """为 durable Run 复制模型边界和预算，不共享运行期状态。"""
        runtime = copy.copy(self)
        runtime.runtime_run_id = runtime_run_id
        runtime.model_provider = ModelProvider(model_provider or self.model_provider)
        runtime.model_name = model_name or self.model_name
        runtime._remaining_token_budget = token_budget
        runtime._token_budget_lock = asyncio.Lock()
        runtime._remaining_model_turns = None
        runtime._reserve_model_turn_callback = None
        runtime._model_turn_budget_enabled = False
        runtime._model_reservations = {}
        runtime._settled_model_reservations = set()
        runtime._model_request_history = {}
        runtime._model_usage_accumulators = {}
        runtime._commit_lock = asyncio.Lock()
        runtime._remaining_deadline_seconds = lambda: float("inf")
        runtime.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        runtime._execution_contract = copy.deepcopy(execution_contract)
        if runtime._execution_contract is None:
            raise V11RuntimeContractError("V11 clone lacks execution contract")
        runtime._validate_execution_contract(
            runtime._execution_contract,
            model_provider=runtime.model_provider,
            model_name=runtime.model_name,
        )
        if (
            isinstance(runtime.model, OpenAICompatibleChatCompletionsModel)
            and runtime.model_name is not None
        ):
            runtime.model = runtime.model.clone_for_model(
                runtime.model_name,
                max_retries=0,
            )
            runtime.model.structured_output_transport = runtime._execution_contract[
                "capability_identity"
            ].get("structured_output_transport", "native_json_schema")
        limits = runtime._execution_contract["limits"]
        runtime.max_turns = int(limits["max_turns"])
        runtime.max_investigators = int(limits["max_investigators"])
        runtime.max_rounds = int(limits["max_rounds"])
        runtime.max_total_tool_calls = int(runtime._execution_contract["tool_budget"])
        runtime.max_tool_calls_per_specialist = int(
            limits["max_tool_calls_per_specialist"]
        )
        runtime.tool_timeout_seconds = float(limits["tool_timeout_seconds"])
        runtime._deadline_at = datetime.now(UTC) + timedelta(
            seconds=runtime.timeout_seconds
        )
        runtime._phase_tool_budget = None
        runtime._check_execution = lambda: None
        runtime._persist_tool_start = None
        runtime._persist_tool_result = None
        runtime._resolve_tool_result = None
        runtime._persist_agent_event = None
        runtime._persist_model_event = None
        runtime._parallel_limit = RunStepGate(runtime.max_investigators)
        runtime._active_sessions = set()
        runtime._failures = []
        runtime._terminal_failure = False
        runtime._terminal_failure_reason = None
        runtime._input_tokens = 0
        runtime._output_tokens = 0
        runtime._completed_rounds = 0
        return runtime

    def bind_phase(self, phase_input: Any) -> None:
        """绑定当前 phase 的 Runtime fence、写入回调与冻结预算。"""
        self.runtime_run_id = phase_input.run_id
        if phase_input.model_provider is not None:
            self.model_provider = ModelProvider(phase_input.model_provider)
        if phase_input.model_name is not None:
            self.model_name = phase_input.model_name
        self._check_execution = phase_input.check_execution or (lambda: None)
        self._resolve_tool_result = phase_input.resolve_tool_result
        self._persist_tool_start = phase_input.persist_tool_start
        self._persist_tool_result = phase_input.persist_tool_result
        self._persist_agent_event = phase_input.persist_agent_event
        self._persist_model_event = phase_input.persist_model_event
        self._model_request_history = self._load_model_request_history(
            getattr(phase_input, "model_events", ())
        )
        self._hit_fault = phase_input.hit_fault or (lambda _point: None)
        self._phase_tool_budget = phase_input.tool_budget
        self._remaining_model_turns = getattr(
            phase_input.resume_state, "remaining_model_turns", None
        )
        self._reserve_model_turn_callback = getattr(
            phase_input, "reserve_model_turn", None
        )
        self._execution_contract = copy.deepcopy(phase_input.execution_contract)
        if getattr(phase_input, "execution_contract_version", None) == "v11":
            # 正式 RuntimeCoordinator 总是注入 Store CAS；无 Store 的单元 phase
            # 只测试模型协议，不能把非持久化计数器冒充正式预算。
            self._model_turn_budget_enabled = (
                self._reserve_model_turn_callback is not None
            )
            if (
                self._reserve_model_turn_callback is None
                and phase_input.persist_model_event is not None
            ):
                raise V11RuntimeContractError(
                    "V11 durable model turn reservation is not wired"
                )
            if self._execution_contract is None:
                raise V11RuntimeContractError("V11 phase lacks execution contract")
            self._validate_execution_contract(
                self._execution_contract,
                model_provider=phase_input.model_provider,
                model_name=phase_input.model_name,
            )
            limits = self._execution_contract["limits"]
            self.max_turns = int(limits["max_turns"])
            self.max_investigators = int(limits["max_investigators"])
            self.max_rounds = int(limits["max_rounds"])
            self.max_total_tool_calls = int(self._execution_contract["tool_budget"])
            self.max_tool_calls_per_specialist = int(
                limits["max_tool_calls_per_specialist"]
            )
            self.tool_timeout_seconds = float(limits["tool_timeout_seconds"])
        self._deadline_at = phase_input.deadline_at or (
            datetime.now(UTC) + timedelta(seconds=self.timeout_seconds)
        )
        self._remaining_deadline_seconds = (
            phase_input.remaining_deadline_seconds or (lambda: float("inf"))
        )
        if phase_input.token_budget is not None:
            if self._remaining_token_budget is None:
                self._remaining_token_budget = phase_input.token_budget
            else:
                self._remaining_token_budget = min(
                    self._remaining_token_budget, phase_input.token_budget
                )
        if phase_input.timeout_seconds is not None:
            self.timeout_seconds = min(self.timeout_seconds, phase_input.timeout_seconds)

    @property
    def remaining_token_budget(self) -> int | None:
        return self._remaining_token_budget

    @property
    def remaining_model_turns(self) -> int | None:
        return self._remaining_model_turns

    async def _reserve_run_model_turn(self) -> None:
        if not self._model_turn_budget_enabled:
            return
        callback = self._reserve_model_turn_callback
        if callback is None:
            raise V11RuntimeContractError(
                "V11 durable model turn reservation is not wired"
            )
        remaining = await _maybe_await(callback())
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
            raise V11RuntimeContractError(
                "V11 durable model turn reservation returned an invalid remainder"
            )
        self._remaining_model_turns = remaining

    def _model_turn_audit_payload(self) -> dict[str, int]:
        if not self._model_turn_budget_enabled:
            return {}
        if self._remaining_model_turns is None:
            raise V11RuntimeContractError(
                "V11 durable model turn budget is missing"
            )
        return {
            "model_turn_budget": self.max_turns,
            "remaining_model_turns": self._remaining_model_turns,
        }

    async def run_phase(
        self,
        phase: RuntimePhase | str,
        *,
        repository,
        investigation_id: str,
        event: IncidentEvent | None = None,
    ) -> Any:
        """按持久化 V11 phase 调用唯一 runtime，不触碰固定 V10 loops。"""
        phase = RuntimePhase(phase)
        current = event or repository.get(investigation_id).event
        run_id = self.runtime_run_id
        if not run_id:
            raise V11RuntimeContractError("V11 phase lacks runtime owner")
        self._validate_bound_execution_contract()
        try:
            with current_investigation_scope(investigation_id):
                return await self._dispatch_phase(
                    phase=phase,
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                    runtime_run_id=run_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            diagnostic = safe_exception_diagnostic(exc, Path.cwd())
            logger.warning(
                "v11 phase failed phase=%s exception_type=%s message=%s location=%s",
                phase.value,
                diagnostic["exception_type"],
                diagnostic["message"],
                diagnostic["location"],
            )
            self._terminalize_unexpected_failure(repository, investigation_id)
            raise

    async def _dispatch_phase(
        self,
        *,
        phase: RuntimePhase,
        repository,
        investigation_id: str,
        event: IncidentEvent,
        runtime_run_id: str,
    ) -> Any:
        if repository.get(investigation_id).status == InvestigationStatus.FAILED:
            return repository.get(investigation_id)
        if phase == RuntimePhase.LEAD_PLANNING:
            return await self.plan_lead(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
                runtime_run_id=runtime_run_id,
                remaining_tool_budget=self._remaining_tool_budget_for(
                    repository, investigation_id
                ),
                remaining_token_budget=self._remaining_token_budget,
            )
        if phase == RuntimePhase.INVESTIGATOR_ROUND_1:
            return await self.investigator_round_1(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase == RuntimePhase.CRITIC_REVIEW:
            return await self.critic_review(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase == RuntimePhase.INVESTIGATOR_ROUND_2:
            return await self.investigator_round_2(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase == RuntimePhase.CRITIC_RECONCILIATION:
            return await self.critic_reconciliation(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase == RuntimePhase.LEAD_ADJUDICATION:
            return await self.lead_adjudication(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase == RuntimePhase.RESULT_VALIDATION:
            return await self.result_validation(
                repository=repository,
                investigation_id=investigation_id,
                event=event,
            )
        if phase in {
            RuntimePhase.INTAKE,
            RuntimePhase.EVIDENCE_COLLECTION,
            RuntimePhase.REPORT_GENERATION,
        }:
            return None
        if phase == RuntimePhase.FINALIZE:
            return self.finalize(repository, investigation_id)
        raise V11RuntimeContractError(f"unsupported V11 phase: {phase.value}")

    async def plan_lead(
        self,
        *,
        repository,
        investigation_id: str,
        event: IncidentEvent,
        runtime_run_id: str,
        remaining_tool_budget: int,
        remaining_token_budget: int | None,
    ) -> DiagnosisPlan:
        """调用 Lead 生成并立即持久化 bounded Investigator plan。"""
        if remaining_tool_budget <= 0:
            # Planning 本身不调用工具，但必须至少为 Investigator 留出一个
            # tool budget；后续 Critic/Lead adjudication 是纯模型阶段，不受
            # Investigator 工具额度耗尽影响。
            raise V11RuntimeContractError("remaining tool budget must be positive")
        if remaining_token_budget is not None and remaining_token_budget < 0:
            raise V11RuntimeContractError("remaining budget must be non-negative")
        self.runtime_run_id = runtime_run_id
        self._remaining_token_budget = remaining_token_budget
        manifest = self._agent_manifest()
        prompt = self._lead_prompt(event, manifest, remaining_tool_budget)
        context = (
            {
                "remaining_tool_budget": remaining_tool_budget,
                "remaining_token_budget": remaining_token_budget,
            }
            if self.turn is None
            else {
                "incident": _event_projection(event),
                "tool_manifest": manifest,
                "remaining_tool_budget": remaining_tool_budget,
                "remaining_token_budget": remaining_token_budget,
            }
        )
        turn = await self._call_model(
            actor=ExecutionActor.LEAD.value,
            prompt=prompt,
            output_type=(
                LeadPlanningCompactOutput
                if self.turn is None
                else LeadPlanningOutput
            ),
            context=context,
            tools=[],
            remaining_token_budget=remaining_token_budget,
            remaining_tool_budget=remaining_tool_budget,
            repository=repository,
            investigation_id=investigation_id,
            task_id=f"lead-planning-{runtime_run_id}",
            step_kind=ExecutionStepKind.LEAD_PLANNING,
            analysis_round=1,
        )
        parsed_output = self._parse_output(
            turn.output,
            LeadPlanningCompactOutput if self.turn is None else LeadPlanningOutput,
        )
        parsed = (
            LeadPlanningOutput(
                decision=parsed_output.decision,
                tasks=[
                    LeadPlanningTaskDraft(
                        id=task.id,
                        title=task.title,
                        description=task.description,
                        analysis_round=1,
                        expected_discriminator=task.expected_discriminator,
                        information_gap=task.information_gap,
                    )
                    for task in parsed_output.tasks
                ],
            )
            if isinstance(parsed_output, LeadPlanningCompactOutput)
            else parsed_output
        )
        plan = self._build_plan(
            parsed,
            investigation_id=investigation_id,
            runtime_run_id=runtime_run_id,
            manifest=manifest,
        )
        # 计划是 Investigator 工作的唯一入口，必须先于任何工作持久化。
        repository.save_plan(plan)
        self._update_summary(repository, investigation_id)
        return plan

    def _build_plan(
        self,
        output: LeadPlanningOutput,
        *,
        investigation_id: str,
        runtime_run_id: str,
        manifest: tuple[str, ...],
    ) -> DiagnosisPlan:
        if output.decision.action == LeadAction.CONCLUDE:
            raise V11RuntimeContractError("planning cannot conclude")
        if output.decision.action == LeadAction.INCONCLUSIVE:
            if output.tasks:
                raise V11RuntimeContractError(
                    "inconclusive planning cannot include tasks"
                )
            return DiagnosisPlan(
                investigation_id=investigation_id,
                runtime_run_id=runtime_run_id,
                tasks=[],
                lead_decision=output.decision,
            )
        if not 1 <= len(output.tasks) <= self.max_investigators:
            raise V11RuntimeContractError("Lead planning task count is out of bounds")
        task_ids = [task.id for task in output.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise V11RuntimeContractError("Lead planning task IDs must be unique")
        if set(output.decision.task_ids) != set(task_ids):
            raise V11RuntimeContractError("Lead decision task ownership mismatch")
        if any(task.analysis_round != 1 for task in output.tasks):
            raise V11RuntimeContractError("planning tasks must start at round one")
        if any(
            tool_name not in manifest
            for task in output.tasks
            for tool_name in task.tool_names
        ):
            raise V11RuntimeContractError("planning task uses a tool outside manifest")
        if any(
            len(set(task.tool_names)) != len(task.tool_names)
            for task in output.tasks
        ):
            raise V11RuntimeContractError("planning task tools must be unique")
        if any(
            not task.evidence_scope and not task.information_gap
            for task in output.tasks
        ):
            raise V11RuntimeContractError("planning task lacks evidence scope")
        if output.decision.action == LeadAction.TEST and any(
            not task.expected_discriminator for task in output.tasks
        ):
            raise V11RuntimeContractError("test planning requires discriminators")
        allowed_skills = {f"{skill.name}@{skill.version}" for skill in self.skills}
        if not set(output.decision.selected_skills) <= allowed_skills:
            raise V11RuntimeContractError("planning selected an unknown skill")
        task_id_by_draft_id = {
            task.id: self._new_task_id() for task in output.tasks
        }
        tasks = [
            DiagnosisTask(
                # 模型 task id 只用于本次 structured output 的引用；持久化主键
                # 必须由服务端生成，否则不同调查的常见 task-1/task-2 会冲突。
                id=task_id_by_draft_id[task.id],
                title=task.title,
                description=task.description,
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name=ExecutionActor.INVESTIGATOR.value,
                # Investigator 在每个 instance 上共享同一冻结 manifest。
                tool_names=list(manifest),
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                analysis_round=1,
                strategy=task.strategy,
                evidence_scope=(
                    task.evidence_scope.model_dump(mode="json")
                    if task.evidence_scope is not None
                    else None
                ),
                expected_discriminator=task.expected_discriminator,
                information_gap=task.information_gap,
                runtime_run_id=runtime_run_id,
            )
            for task in output.tasks
        ]
        decision = output.decision.model_copy(
            update={
                "task_ids": [task_id_by_draft_id[task_id] for task_id in task_ids],
                "candidate_ids": [],
            }
        )
        return DiagnosisPlan(
            investigation_id=investigation_id,
            runtime_run_id=runtime_run_id,
            tasks=tasks,
            lead_decision=decision,
        )

    @staticmethod
    def _new_task_id() -> str:
        """为持久化任务生成不受模型控制的全局唯一主键。"""
        return f"task-{uuid4().hex}"

    @staticmethod
    def _new_candidate_id() -> str:
        """为持久化候选生成不受模型控制的全局唯一主键。"""
        return f"candidate-{uuid4().hex}"

    def _agent_manifest(self) -> tuple[str, ...]:
        if self.tool_registry is None:
            raise V11RuntimeContractError("V11 tool registry is not configured")
        contract = getattr(self, "_execution_contract", None)
        if contract is not None:
            self._validate_execution_contract(
                contract,
                model_provider=self.model_provider,
                model_name=self.model_name,
            )
            raw_manifest = contract.get("tool_manifest")
            if not isinstance(raw_manifest, (list, tuple)):
                raise V11RuntimeContractError("V11 contract lacks frozen tool manifest")
            manifest = tuple(raw_manifest)
            try:
                for tool_name in manifest:
                    self.tool_registry.assert_agent_callable(tool_name, manifest)
            except (TypeError, ValueError) as exc:
                raise V11RuntimeContractError(
                    "V11 frozen tool manifest is not callable"
                ) from exc
            return manifest
        manifest = self.tool_registry.agent_manifest()
        if len(manifest) != 9:
            raise V11RuntimeContractError("V11 requires exactly nine Agent tools")
        return manifest

    def _validate_bound_execution_contract(self) -> None:
        contract = getattr(self, "_execution_contract", None)
        if contract is not None:
            self._validate_execution_contract(
                contract,
                model_provider=self.model_provider,
                model_name=self.model_name,
            )

    def _validate_execution_contract(
        self,
        contract: dict[str, Any],
        *,
        model_provider: ModelProvider | None,
        model_name: str | None,
    ) -> None:
        try:
            validate_v11_execution_contract(contract)
        except ValueError as exc:
            raise V11RuntimeContractError(str(exc)) from exc
        expected_provider = (
            model_provider.value
            if isinstance(model_provider, ModelProvider)
            else model_provider
        )
        if contract["model_provider"] != expected_provider:
            raise V11RuntimeContractError("V11 execution contract provider mismatch")
        if contract["model_name"] != model_name:
            raise V11RuntimeContractError("V11 execution contract model mismatch")
        if expected_provider == ModelProvider.OPENAI.value:
            if contract["api_mode"] != "responses" or contract["endpoint_id"] != endpoint_id(
                OFFICIAL_OPENAI_BASE_URL
            ):
                raise V11RuntimeContractError("V11 official endpoint contract mismatch")
        if (
            expected_provider == ModelProvider.OPENAI_COMPATIBLE.value
            and isinstance(self.model, OpenAICompatibleChatCompletionsModel)
        ):
            try:
                actual_endpoint_id = endpoint_id(
                    canonicalize_endpoint(self.model._base_url)
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise V11RuntimeContractError(
                    "V11 compatible client endpoint is unavailable"
                ) from exc
            if contract["endpoint_id"] != actual_endpoint_id:
                raise V11RuntimeContractError("V11 compatible endpoint contract mismatch")
        capability = contract["capability_identity"]
        if (
            capability["provider"],
            capability["model"],
            capability["api_mode"],
            capability["endpoint_id"],
            capability["artifact_hash"],
        ) != (
            contract["model_provider"],
            contract["model_name"],
            contract["api_mode"],
            contract["endpoint_id"],
            contract["capability_artifact_hash"],
        ):
            raise V11RuntimeContractError("V11 capability identity projection mismatch")
        if (
            expected_provider == ModelProvider.OPENAI_COMPATIBLE.value
            and isinstance(self.model, OpenAICompatibleChatCompletionsModel)
            and capability.get("structured_output_transport")
            not in {"native_json_schema", "strict_output_tool"}
        ):
            raise V11RuntimeContractError(
                "V11 structured output transport is invalid"
            )
        limits = contract["limits"]
        if int(limits["max_turns"]) < 1 or int(limits["max_tool_calls_per_specialist"]) < 1:
            raise V11RuntimeContractError("V11 actor limit is out of bounds")
        try:
            manifest = tuple(contract["tool_manifest"])
            if len(manifest) != 9 or len(set(manifest)) != len(manifest):
                raise ValueError("V11 requires exactly nine frozen Agent tools")
            if manifest != tuple(sorted(manifest)):
                raise ValueError("V11 frozen tool manifest is not ordered")
            if contract["tool_manifest_hash"] != agent_manifest_hash(manifest):
                raise ValueError("V11 frozen tool manifest hash mismatch")
            expected_skill = {
                "catalog_version": SKILL_CATALOG_VERSION,
                "catalog_hash": skill_catalog_hash(self.skills),
                "skill_names": ",".join(
                    f"{skill.name}@{skill.version}" for skill in self.skills
                ),
            }
            if contract["skill_catalog"] != expected_skill:
                raise ValueError("skill catalog")
        except (KeyError, TypeError, ValueError) as exc:
            raise V11RuntimeContractError(str(exc)) from exc

    async def investigator_round_1(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> tuple[AgentFinding, ...]:
        """执行最多三个相互隔离的通用 Investigator。"""
        plan = repository.get_plan(investigation_id)
        if plan is None:
            raise V11RuntimeContractError("investigator round one lacks Lead plan")
        tasks = [
            task
            for task in repository.list_tasks(investigation_id)
            if task.analysis_round == 1
        ]
        base_evidence = list(repository.get(investigation_id).evidence)
        candidates: list[RootCauseCandidate] = []
        selected_tasks = tasks[: self.max_investigators]

        async def run_task(task: DiagnosisTask) -> _InvestigatorResult:
            try:
                self._check_execution()
                async with self._parallel_limit.slot():
                    return await self._run_investigator(
                        repository=repository,
                        investigation_id=investigation_id,
                        event=event,
                        task=task,
                        round_number=1,
                        seed_evidence=_evidence_for_task(task, base_evidence),
                        own_findings=(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures.append(type(exc).__name__)
                return _InvestigatorResult(
                    (),
                    (),
                    self._failed_execution(
                        task_id=task.id,
                        actor=ExecutionActor.INVESTIGATOR.value,
                        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                        message="investigator failed",
                        analysis_round=1,
                    ),
                )

        results = await asyncio.gather(*(run_task(task) for task in selected_tasks))
        for result in results:
            candidates.extend(result.candidates)
            self._persist_investigator_result(repository, investigation_id, result)
        if tasks and not any(
            result.execution.status == AgentExecutionStatus.COMPLETED
            for result in results
        ):
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "all investigators failed",
            )
            return ()
        self._completed_rounds = max(self._completed_rounds, 1)
        self._persist_candidate_projection(repository, investigation_id, candidates)
        self._update_summary(repository, investigation_id)
        return tuple(item for result in results for item in result.findings)

    async def critic_review(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview:
        """对每个候选执行一次、且恰好七项检查的 Critic review。"""
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            review = self._empty_review(repository, investigation_id)
        if not review.candidates:
            repository.save_coordination_review(review)
            self._update_summary(repository, investigation_id)
            return review
        try:
            remaining_tool_budget = self._remaining_tool_budget_for(
                repository, investigation_id
            )
            output_type = (
                CriticCompactOutput
                if self.turn is None and len(review.candidates) <= self.max_investigators
                else CriticOutput
            )
            turn = await self._call_model(
                actor=ExecutionActor.CRITIC.value,
                prompt=self._critic_prompt(
                    repository, investigation_id, event, review, round_number=1
                ),
                output_type=output_type,
                context=self._critic_context(
                    repository, investigation_id, review, round_number=1
                ),
                tools=[],
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=remaining_tool_budget,
                repository=repository,
                investigation_id=investigation_id,
                task_id=f"critic-review-{self.runtime_run_id}",
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                analysis_round=1,
            )
            output = self._parse_output(turn.output, output_type)
            assessments = self._normalize_assessments(
                output.assessments,
                candidate_ids={item.id for item in review.candidates},
                runtime_run_id=self.runtime_run_id or "",
                review_round=1,
                usable_evidence_ids={
                    item.id
                    for item in repository.get(investigation_id).evidence
                    if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
                    and item.runtime_run_id == self.runtime_run_id
                },
            )
            tasks, supplemental_task_id_map = self._supplemental_tasks(
                output,
                assessments,
                runtime_run_id=self.runtime_run_id or "",
            )
            assessments = [
                item.model_copy(
                    update={
                        "supplemental_task_ids": [
                            supplemental_task_id_map[task_id]
                            for task_id in item.supplemental_task_ids
                        ]
                    }
                )
                for item in assessments
            ]
            assessments, tasks = self._bound_supplemental_work(
                assessments,
                tasks,
                remaining_tool_budget=remaining_tool_budget,
            )
            existing_tasks = repository.list_tasks(investigation_id)
            repository.save_tasks(investigation_id, [*existing_tasks, *tasks])
            review = review.model_copy(
                update={
                    "critic_assessments": assessments,
                    "summary": getattr(output, "summary", "")
                    or "Critic review completed.",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            failure_message, failure_category = _critic_failure_audit(exc)
            self._ensure_failed_execution(
                repository,
                investigation_id,
                task_id=f"critic-review-{self.runtime_run_id}",
                actor=ExecutionActor.CRITIC.value,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                message=failure_message,
                failure_category=failure_category,
            )
            review = review.model_copy(
                update={
                    "critic_assessments": [],
                    "lead_decision": None,
                    "diagnostic_status": None,
                    "run_status": MultiAgentRunStatus.FAILED,
                    "stop_reason": failure_message.replace(" ", "_"),
                    "summary": "Critic review failed.",
                }
            )
            repository.save_coordination_review(review)
            self._mark_terminal_failure(
                repository,
                investigation_id,
                failure_message,
                preserve_candidates=True,
            )
            return review
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    async def investigator_round_2(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> tuple[AgentFinding, ...]:
        """只执行 Critic needs_evidence 产生的那一批 round2 task。"""
        tasks = [
            task
            for task in repository.list_tasks(investigation_id)
            if task.analysis_round == 2
        ]
        if not tasks:
            return ()
        evidence = list(repository.get(investigation_id).evidence)
        findings: list[AgentFinding] = []
        prepared: list[tuple[DiagnosisTask, CriticAssessment]] = []
        for task in tasks[: self.max_investigators]:
            review = repository.get_coordination_review(investigation_id)
            assessment = next(
                (
                    item
                    for item in (review.critic_assessments if review is not None else [])
                    if item.id == task.critic_assessment_id
                ),
                None,
            )
            if assessment is None:
                self._failures.append("missing_assessment")
                continue
            prepared.append((task, assessment))

        selected_task_count = min(len(tasks), self.max_investigators)
        if len(prepared) != selected_task_count:
            self._ensure_failed_execution(
                repository,
                investigation_id,
                task_id=f"round-two-contract-{self.runtime_run_id}",
                actor=ExecutionActor.INVESTIGATOR.value,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                message="round two task assessment ownership is incomplete",
                analysis_round=2,
            )
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "round two task assessment ownership failed",
            )
            return ()

        async def run_task(
            task_and_assessment: tuple[DiagnosisTask, CriticAssessment]
        ) -> _InvestigatorResult:
            task, assessment = task_and_assessment
            try:
                self._check_execution()
                async with self._parallel_limit.slot():
                    return await self._run_investigator(
                        repository=repository,
                        investigation_id=investigation_id,
                        event=event,
                        task=task,
                        round_number=2,
                        seed_evidence=_evidence_for_task(task, evidence),
                        own_findings=tuple(
                            item
                            for item in repository.list_agent_findings(investigation_id)
                            if item.task_id == task.id
                        ),
                        assessment=assessment,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures.append(type(exc).__name__)
                return _InvestigatorResult(
                    (),
                    (),
                    self._failed_execution(
                        task_id=task.id,
                        actor=ExecutionActor.INVESTIGATOR.value,
                        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                        message="investigator failed",
                        analysis_round=2,
                    ),
                )

        results = await asyncio.gather(*(run_task(item) for item in prepared))
        for result in results:
            findings.extend(result.findings)
            self._persist_investigator_result(repository, investigation_id, result)
        if any(
            result.execution.status != AgentExecutionStatus.COMPLETED
            for result in results
        ):
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "round two investigator failed",
            )
            return ()
        self._completed_rounds = 2
        self._update_summary(repository, investigation_id)
        return tuple(findings)

    async def critic_reconciliation(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview | None:
        """对同一批 assessment 做唯一一次 reconciliation，禁止第三轮。"""
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            return None
        round_two_tasks = [
            task for task in repository.list_tasks(investigation_id) if task.analysis_round == 2
        ]
        if not round_two_tasks:
            return review
        try:
            turn = await self._call_model(
                actor=ExecutionActor.CRITIC.value,
                prompt=self._critic_prompt(
                    repository, investigation_id, event, review, round_number=2
                ),
                output_type=CriticOutput,
                context=self._critic_context(
                    repository, investigation_id, review, round_number=2
                ),
                tools=[],
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(
                    repository, investigation_id
                ),
                repository=repository,
                investigation_id=investigation_id,
                task_id=f"critic-reconciliation-{self.runtime_run_id}",
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                analysis_round=2,
            )
            output = self._parse_output(turn.output, CriticOutput)
            assessments = self._normalize_assessments(
                output.assessments,
                candidate_ids={item.id for item in review.candidates},
                runtime_run_id=self.runtime_run_id or "",
                review_round=2,
                usable_evidence_ids={
                    item.id
                    for item in repository.get(investigation_id).evidence
                    if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
                    and item.runtime_run_id == self.runtime_run_id
                },
                existing_assessments=review.critic_assessments,
            )
            expected = {item.id for item in review.critic_assessments}
            if {item.id for item in assessments} != expected:
                raise V11RuntimeContractError("reconciliation must reuse assessment IDs")
            if output.tasks or any(
                item.verdict.value == "needs_evidence" for item in assessments
            ):
                raise V11RuntimeContractError(
                    "reconciliation cannot request a third round"
                )
            review = review.model_copy(
                update={
                    "critic_assessments": assessments,
                    "summary": output.summary or "Critic reconciliation completed.",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            failure_message, failure_category = _critic_failure_audit(exc)
            self._ensure_failed_execution(
                repository,
                investigation_id,
                task_id=f"critic-reconciliation-{self.runtime_run_id}",
                actor=ExecutionActor.CRITIC.value,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                message=failure_message,
                analysis_round=2,
                failure_category=failure_category,
            )
            review = review.model_copy(
                update={
                    "lead_decision": None,
                    "diagnostic_status": None,
                    "run_status": MultiAgentRunStatus.FAILED,
                    "stop_reason": failure_message.replace(" ", "_"),
                    "summary": "Critic reconciliation failed.",
                }
            )
            repository.save_coordination_review(review)
            self._mark_terminal_failure(
                repository,
                investigation_id,
                failure_message,
                preserve_candidates=True,
            )
            return review
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    async def lead_adjudication(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview:
        """Lead 只可采纳 Critic accepted IDs；无候选时结论必须 inconclusive。"""
        self._restore_failure_memory(repository, investigation_id)
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            review = self._empty_review(repository, investigation_id)
        decision: LeadDecision | None = None
        try:
            if self.turn is None:
                # Critic 已经返回服务端 candidate_ref；live authority 只做
                # 机械的 verdict→candidate ID 投影，不再让第二个模型重复
                # 生成同一组 authority refs 并消耗冻结预算。
                decision = self._critic_authority_decision(review)
            else:
                turn = await self._call_model(
                    actor=ExecutionActor.LEAD.value,
                    prompt=self._lead_adjudication_prompt(repository, review, event),
                    output_type=LeadAdjudicationOutput,
                    context=self._lead_adjudication_context(repository, review),
                    tools=[],
                    remaining_token_budget=self._remaining_token_budget,
                    remaining_tool_budget=self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                    repository=repository,
                    investigation_id=investigation_id,
                    task_id=f"lead-adjudication-{self.runtime_run_id}",
                    step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                    analysis_round=2 if self._completed_rounds == 2 else 1,
                )
                decision = self._parse_output(
                    turn.output, LeadAdjudicationOutput
                ).decision
            self._validate_lead_decision(decision, review)
        except asyncio.CancelledError:
            raise
        except Exception as first_error:
            self._failures.append(type(first_error).__name__)
            # 只允许一次无工具 correction；不会重跑 Critic 或 Investigator。
            try:
                turn = await self._call_model(
                    actor=ExecutionActor.LEAD.value,
                    prompt=(
                        self._lead_adjudication_prompt(repository, review, event)
                        + (
                            "\nCORRECTION=Return only an inconclusive decision "
                            "if any reference is uncertain."
                        )
                    ),
                    output_type=LeadAdjudicationOutput,
                    context={
                        **self._lead_adjudication_context(repository, review),
                        "correction_attempt": 1,
                    },
                    tools=[],
                    remaining_token_budget=self._remaining_token_budget,
                    remaining_tool_budget=self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                    repository=repository,
                    investigation_id=investigation_id,
                    task_id=f"lead-adjudication-{self.runtime_run_id}-correction",
                    step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                    analysis_round=2 if self._completed_rounds == 2 else 1,
                )
                decision = self._parse_output(
                    turn.output, LeadAdjudicationOutput
                ).decision
                self._validate_lead_decision(decision, review)
            except asyncio.CancelledError:
                raise
            except Exception as second_error:
                self._failures.append(type(second_error).__name__)
                self._ensure_failed_execution(
                    repository,
                    investigation_id,
                    task_id=f"lead-adjudication-{self.runtime_run_id}-correction",
                    actor=ExecutionActor.LEAD.value,
                    step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                    message="lead correction failed",
                    analysis_round=2 if self._completed_rounds == 2 else 1,
                )
        if decision is None:
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "lead adjudication failed",
            )
            review = review.model_copy(
                update={
                    "candidates": [],
                    "critic_assessments": [],
                    "lead_decision": None,
                    "diagnostic_status": None,
                    "run_status": MultiAgentRunStatus.FAILED,
                    "stop_reason": "lead_adjudication_failed",
                    "summary": "Lead adjudication failed.",
                }
            )
            repository.save_coordination_review(review)
            return review
        status = self._diagnostic_status(decision)
        projection = {
            "lead_decision": decision,
            "diagnostic_status": status,
            "stop_reason": decision.stop_reason,
            # 终态矩阵只允许 COMPLETE/COMPLETED、PARTIAL/PARTIAL、
            # INCONCLUSIVE/COMPLETED 三种组合。
            "run_status": (
                MultiAgentRunStatus.PARTIAL
                if status == DiagnosticStatus.PARTIAL
                else MultiAgentRunStatus.COMPLETED
            ),
            "summary": decision.summary,
        }
        accepted_ids = {
            item.candidate_id
            for item in review.critic_assessments
            if item.verdict == CriticVerdict.ACCEPT
        }
        unpublished = []
        for candidate in review.candidates:
            if candidate.id in decision.candidate_ids:
                continue
            assessment = next(
                (
                    item
                    for item in review.critic_assessments
                    if item.candidate_id == candidate.id
                ),
                None,
            )
            if assessment is None:
                reason = "critic_assessment_missing"
            elif assessment.verdict != CriticVerdict.ACCEPT:
                reason = f"critic_not_accepted:{assessment.verdict.value}"
            elif candidate.id in accepted_ids:
                reason = "lead_did_not_authorize"
            else:
                reason = "candidate_not_authoritative"
            unpublished.append({"candidate_ref": candidate.id, "reason": reason})
        lead_audit = json.dumps(
            {
                "schema_version": "candidate-lifecycle-authority-v1",
                "persisted_candidate_count": len(review.candidates),
                "authoritative_candidate_ids": list(decision.candidate_ids),
                "unpublished": unpublished,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if self.turn is None:
            # 该 execution 明确标记为 CUSTOM：它记录的是服务端 authority
            # 投影，不冒充一次额外的模型调用；Critic verdict 才是诊断判断来源。
            now = datetime.now(UTC)
            self._record_execution(
                repository,
                investigation_id,
                AgentExecution(
                    id=f"exec-{uuid4().hex}",
                    task_id=f"lead-adjudication-{self.runtime_run_id}",
                    agent_name=ExecutionActor.LEAD.value,
                    runtime_run_id=self.runtime_run_id,
                    status=AgentExecutionStatus.COMPLETED,
                    execution_layer=AgentExecutionLayer.CUSTOM,
                    analysis_round=2 if self._completed_rounds == 2 else 1,
                    step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                    runtime_attempt_id=f"server-{uuid4().hex}",
                    failure_category=FailureCategory.NONE,
                    model_provider=self.model_provider,
                    model_name=self.model_name,
                    summary=lead_audit,
                    started_at=now,
                    completed_at=now,
                ),
            )
        lead_executions = [
            item
            for item in repository.list_executions(investigation_id)
            if item.agent_name == ExecutionActor.LEAD.value
            and item.step_kind == ExecutionStepKind.LEAD_ADJUDICATION
            and item.runtime_run_id == self.runtime_run_id
        ]
        if lead_executions:
            latest = max(
                lead_executions,
                key=lambda item: (
                    item.attempt,
                    item.started_at or datetime.min.replace(tzinfo=UTC),
                    item.id,
                ),
            )
            self._record_execution(
                repository,
                investigation_id,
                latest.model_copy(update={"summary": lead_audit}),
            )
        if decision.action == LeadAction.INCONCLUSIVE:
            # inconclusive 只取消发布权；候选和 critic 评审保留为审计投影，
            # predictions 仍由 authoritative_candidate_ids 严格过滤。
            projection.update({"root_causes": []})
        review = review.model_copy(
            update=projection
        )
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    @staticmethod
    def _critic_authority_decision(review: CoordinationReview) -> LeadDecision:
        """将已校验 Critic verdict 投影为唯一的 live authority decision。"""
        candidate_ids = {candidate.id for candidate in review.candidates}
        accepted = [
            assessment.candidate_id
            for assessment in review.critic_assessments
            if assessment.verdict == CriticVerdict.ACCEPT
            and assessment.candidate_id in candidate_ids
        ]
        if not accepted:
            return LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="No candidate was accepted by Critic.",
                stop_reason="critic_no_accepted_candidate",
            )
        accepted_set = set(accepted)
        evidence_ids = {
            evidence_id
            for candidate in review.candidates
            if candidate.id in accepted_set
            for evidence_id in (
                *candidate.supporting_evidence_ids,
                *candidate.contradicting_evidence_ids,
            )
        }
        evidence_ids.update(
            evidence_id
            for assessment in review.critic_assessments
            if assessment.candidate_id in accepted_set
            for evidence_id in (
                *assessment.supporting_evidence_ids,
                *assessment.contradicting_evidence_ids,
                *(
                    evidence_id
                    for check in assessment.checks
                    for evidence_id in check.evidence_ids
                ),
            )
        )
        return LeadDecision(
            action=LeadAction.CONCLUDE,
            summary="Critic accepted evidence-backed candidates.",
            candidate_ids=accepted,
            evidence_ids=sorted(evidence_ids),
        )

    async def result_validation(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview:
        """机械校验结果，失败时只做一次无工具 Lead correction。"""
        del event
        self._restore_failure_memory(repository, investigation_id)
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            review = self._empty_review(repository, investigation_id)
            review = review.model_copy(
                update={
                    "lead_decision": self._inconclusive_decision(
                        "No candidate was produced."
                    ),
                    "diagnostic_status": DiagnosticStatus.INCONCLUSIVE,
                }
            )
            repository.save_coordination_review(review)
            self._update_summary(repository, investigation_id)
            return review
        try:
            validate_v11_result(
                investigation_id=investigation_id,
                runtime_run_id=self.runtime_run_id or "",
                findings=repository.list_agent_findings(investigation_id),
                candidates=review.candidates,
                review=review,
                executions=repository.list_executions(investigation_id),
                evidence=repository.get(investigation_id).evidence,
                tasks=repository.list_tasks(investigation_id),
                tool_calls=repository.list_tool_calls(investigation_id),
                tool_registry=self.tool_registry,
                agent_manifest=self._agent_manifest(),
                status=review.diagnostic_status,
            )
        except V11ResultValidationError as first_error:
            self._failures.append(first_error.code)
            # Validator 本身不修改任何 Agent 字段，correction 也只能改变 Lead decision。
            try:
                if (
                    self._remaining_token_budget is not None
                    and self._remaining_token_budget <= 0
                ):
                    # 预算耗尽时 correction 必败（预算门 fail-closed）；跳过
                    # 这次必败调用，直接落入下方既有 terminal 路径。
                    self._failures.append("correction_budget_exhausted")
                    raise V11RuntimeContractError("correction budget exhausted")
                turn = await self._call_model(
                    actor=ExecutionActor.LEAD.value,
                    prompt="Return a mechanically valid inconclusive V11 Lead decision.",
                    output_type=LeadAdjudicationOutput,
                    context={"correction_attempt": 1, "reason": first_error.code},
                    tools=[],
                    remaining_token_budget=self._remaining_token_budget,
                    remaining_tool_budget=self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                    repository=repository,
                    investigation_id=investigation_id,
                    task_id=f"lead-result-validation-{self.runtime_run_id}",
                    step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                    analysis_round=2 if self._completed_rounds == 2 else 1,
                )
                corrected = self._parse_output(
                    turn.output, LeadAdjudicationOutput
                ).decision
                self._validate_lead_decision(corrected, review)
                corrected_status = self._diagnostic_status(corrected)
                correction_projection = {
                    "lead_decision": corrected,
                    "diagnostic_status": corrected_status,
                    "stop_reason": corrected.stop_reason,
                    "run_status": (
                        MultiAgentRunStatus.PARTIAL
                        if corrected_status == DiagnosticStatus.PARTIAL
                        else MultiAgentRunStatus.COMPLETED
                    ),
                }
                if corrected.action == LeadAction.INCONCLUSIVE:
                    # inconclusive 只取消发布权；已准入候选继续作为审计投影保留。
                    correction_projection.update({"root_causes": []})
                review = review.model_copy(update=correction_projection)
                repository.save_coordination_review(review)
                validate_v11_result(
                    investigation_id=investigation_id,
                    runtime_run_id=self.runtime_run_id or "",
                    findings=repository.list_agent_findings(investigation_id),
                    candidates=review.candidates,
                    review=review,
                    executions=repository.list_executions(investigation_id),
                    evidence=repository.get(investigation_id).evidence,
                    tasks=repository.list_tasks(investigation_id),
                    tool_calls=repository.list_tool_calls(investigation_id),
                    tool_registry=self.tool_registry,
                    agent_manifest=self._agent_manifest(),
                    status=review.diagnostic_status,
                )
            except asyncio.CancelledError:
                raise
            except Exception as second_error:
                self._failures.append(type(second_error).__name__)
                self._record_execution(
                    repository,
                    investigation_id,
                    self._failed_execution(
                        task_id=f"result-validation-{self.runtime_run_id}",
                        actor="ValidatorAgent",
                        step_kind=ExecutionStepKind.RESULT_VALIDATION,
                        message="result validation correction failed",
                    ),
                )
                self._mark_terminal_failure(
                    repository,
                    investigation_id,
                    "result validation failed",
                )
                review = review.model_copy(
                    update={
                        "candidates": [],
                        "critic_assessments": [],
                        "lead_decision": None,
                        "diagnostic_status": None,
                        "run_status": MultiAgentRunStatus.FAILED,
                        "stop_reason": "result_validation_failed",
                        "summary": "Result validation failed.",
                    }
                )
                repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    def finalize(self, repository, investigation_id: str) -> Any:
        """返回最终状态；不生成 V10 report 或 action。"""
        review = repository.get_coordination_review(investigation_id)
        if review is None or review.diagnostic_status is None:
            return None
        self._update_summary(repository, investigation_id)
        return repository.get(investigation_id)

    async def _run_investigator(
        self,
        *,
        repository,
        investigation_id: str,
        event: IncidentEvent,
        task: DiagnosisTask,
        round_number: int,
        seed_evidence: list[EvidenceItem],
        own_findings: Iterable[AgentFinding],
        assessment: CriticAssessment | None = None,
    ) -> _InvestigatorResult:
        existing = next(
            (
                item
                for item in repository.list_agent_findings(
                    investigation_id
                )
                if item.task_id == task.id and item.analysis_round == round_number
            ),
            None,
        )
        if existing is not None:
            execution = next(
                (
                    item
                    for item in repository.list_executions(
                        investigation_id
                    )
                    if item.task_id == task.id and item.analysis_round == round_number
                ),
                AgentExecution(
                    task_id=task.id,
                    agent_name=ExecutionActor.INVESTIGATOR.value,
                    runtime_run_id=self.runtime_run_id,
                    status=AgentExecutionStatus.COMPLETED,
                    execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                    step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                    analysis_round=round_number,
                ),
            )
            return _InvestigatorResult((existing,), (), execution)
        previous_execution = next(
            (
                item
                for item in repository.list_executions(
                    investigation_id
                )
                if item.task_id == task.id and item.analysis_round == round_number
            ),
            None,
        )
        instance_id = (
            previous_execution.agent_name
            if previous_execution is not None
            and previous_execution.agent_name.startswith("investigator-")
            else f"investigator-{uuid4().hex}"
        )
        manifest = self._agent_manifest()
        plan = repository.get_plan(investigation_id)
        selected_skills = (
            list(plan.lead_decision.selected_skills)
            if plan is not None and plan.lead_decision is not None
            else []
        )
        session = AdaptiveToolSession(
            event=event,
            seed_evidence=seed_evidence,
            registry=self.tool_registry,
            task_ids={instance_id: task.id},
            max_tool_calls_per_specialist=self.max_tool_calls_per_specialist,
            max_total_tool_calls=self._remaining_tool_budget_for(
                repository, investigation_id
            ),
            tool_timeout_seconds=self.tool_timeout_seconds,
            runtime_run_id=self.runtime_run_id,
            resolve_tool_result=self._resolve_tool_result,
            persist_tool_start=self._persist_tool_start,
            persist_tool_result=self._persist_tool_result,
            check_execution=self._check_execution,
            max_parallel_steps_per_run=self.max_investigators,
            hit_fault=self._hit_fault,
            parallel_limit=self._parallel_limit,
            agent_manifest=manifest,
            remaining_deadline_seconds=self._remaining_deadline_seconds,
        )
        self._active_sessions.add(session)
        try:
            evidence_digest = _select_evidence_digest(
                seed_evidence,
                # 每类保留两个有界锚点，避免只看到单个高分信号就把
                # 同一服务的反证/影响指标误判为证据缺口；完整证据仍只
                # 保存在服务端，且候选只能引用本次摘要中的 ID。
                max_per_kind=2,
                max_total=4,
            )
            # live SDK 请求在已有证据时使用紧凑 Draft schema；注入 turn 仍保留
            # 完整 Investigator schema，以便 deterministic 测试覆盖 finding 合同。
            compact_output = bool(evidence_digest) and self.turn is None
            output_type = (
                InvestigatorCandidateOutput if compact_output else InvestigatorOutput
            )
            prompt = self._investigator_prompt(
                event,
                task,
                instance_id,
                manifest,
                evidence_digest,
                own_findings,
                assessment,
                selected_skills,
            )
            model_context = (
                {
                    "round": round_number,
                    "own_committed_evidence_ids": [
                        item.id for item in evidence_digest
                    ],
                    "remaining_tool_budget": self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                }
                if compact_output
                else {
                    "incident": _event_projection(event),
                    "task": _investigator_task_projection(task),
                    "agent_instance_id": instance_id,
                    "round": round_number,
                    "tool_manifest": list(manifest) if not evidence_digest else [],
                    "selected_skills": selected_skills if not evidence_digest else [],
                    "own_committed_evidence_ids": [
                        item.id for item in evidence_digest
                    ],
                    "own_committed_finding_ids": [
                        item.id for item in own_findings
                    ],
                    "assessment_id": assessment.id if assessment is not None else None,
                    "remaining_tool_budget": self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                }
            )
            turn = await self._call_model(
                actor=ExecutionActor.INVESTIGATOR.value,
                prompt=prompt,
                output_type=output_type,
                context=model_context,
                tools=(
                    session.tools_for(instance_id, round_number)
                    if not evidence_digest
                    else []
                ),
                # 并发 Investigator 必须让 reservation 根据剩余槽位均分；传入
                # 整笔剩余预算会让第一个请求独占余量，后续请求被误判 quota。
                remaining_token_budget=None,
                remaining_tool_budget=self._remaining_tool_budget_for(
                    repository, investigation_id
                ),
                repository=repository,
                investigation_id=investigation_id,
                task_id=task.id,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                analysis_round=round_number,
            )
            # 任何 finding 引用前，先收口 tool/evidence 的 durable 投影。
            await self._commit_session(repository, investigation_id, session)
            output = self._parse_output(turn.output, output_type)
            committed_evidence = repository.get(investigation_id).evidence
            candidate_drafts = tuple(output.candidates)
            # 模型输出是不可信数据：单个 draft 违约只拒绝该 draft 并留持久化
            # 审计（spec §7.4 校验器可拒绝输出），不让同批合法 finding 陪葬。
            findings: list[AgentFinding] = []
            audit_executions: list[AgentExecution] = []
            rejected = 0
            for draft in (
                output.findings
                if isinstance(output, InvestigatorOutput)
                else ()
            ):
                try:
                    findings.append(
                        self._finding_from_draft(
                            draft,
                            investigation_id=investigation_id,
                            task=task,
                            instance_id=instance_id,
                            round_number=round_number,
                            assessment=assessment,
                            evidence=committed_evidence,
                        )
                    )
                except V11RuntimeContractError as exc:
                    rejected += 1
                    self._failures.append("investigator_finding_draft_rejected")
                    audit_executions.append(
                        self._failed_execution(
                            task_id=task.id,
                            actor=instance_id,
                            step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                            message=_draft_rejection_code(exc),
                            analysis_round=round_number,
                            failure_category=FailureCategory.INVALID_REFERENCE,
                        )
                    )
            candidates = (
                self._admit_candidates(
                    candidate_drafts,
                    batch_findings=findings,
                    committed_evidence=committed_evidence,
                    repository=repository,
                    investigation_id=investigation_id,
                    task=task,
                    instance_id=instance_id,
                    audit_executions=audit_executions,
                )
                if round_number == 1
                else ()
            )
            if rejected and not findings and not candidates:
                # 整批违约全灭：维持现行 investigator 失败语义（single 配置下
                # 由 round 层的 all-failed 检查收敛 terminal）。
                self._failures.append("investigator_batch_rejected")
                return _InvestigatorResult(
                    (),
                    (),
                    self._failed_execution(
                        task_id=task.id,
                        actor=instance_id,
                        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                        message="investigator findings rejected",
                        analysis_round=round_number,
                    ),
                    tuple(audit_executions),
                )
            execution = next(
                (
                    item
                    for item in repository.list_executions(investigation_id)
                    if item.id == turn.execution_id
                ),
                None,
            )
            if execution is None:
                execution = AgentExecution(
                    task_id=task.id,
                    agent_name=instance_id,
                    runtime_run_id=self.runtime_run_id,
                    status=AgentExecutionStatus.COMPLETED,
                    execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                    analysis_round=round_number,
                    step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                    model_provider=self.model_provider,
                    model_name=self.model_name,
                )
            execution = execution.model_copy(
                update={
                    "task_id": task.id,
                    "agent_name": instance_id,
                    "tool_call_ids": [call.id for call in session.tool_calls],
                    "evidence_ids": sorted(
                        session.evidence_ids_for(instance_id, round_number)
                    ),
                    "summary": _candidate_lifecycle_trace(
                        raw_output=turn.output,
                        parsed_drafts=candidate_drafts,
                        admitted_candidates=candidates,
                        failure_categories=(
                            audit.error_message or audit.failure_category.value
                            for audit in audit_executions
                        ),
                    ),
                }
            )
            return _InvestigatorResult(
                tuple(findings), candidates, execution, tuple(audit_executions)
            )
        except asyncio.CancelledError:
            self._cleanup_session(session)
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            failure_message, failure_category = _investigator_failure_audit(exc)
            execution = self._failed_execution(
                task_id=task.id,
                actor=instance_id,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                message=failure_message,
                analysis_round=round_number,
                failure_category=failure_category,
            )
            return _InvestigatorResult((), (), execution)
        finally:
            self._cleanup_session(session)

    def _admit_candidates(
        self,
        drafts: tuple[InvestigatorCandidateDraft | RootCauseCandidate, ...],
        *,
        batch_findings: list[AgentFinding],
        committed_evidence: list[EvidenceItem],
        repository,
        investigation_id: str,
        task: DiagnosisTask,
        instance_id: str,
        audit_executions: list[AgentExecution],
    ) -> tuple[RootCauseCandidate, ...]:
        """candidate 草稿的准入校验：违约候选丢弃并留审计，绝不改写引用。

        finding ID 在服务端生成，模型填的引用几乎必然无效；终态校验
        （candidate_finding_reference / candidate_evidence_reference）会把
        这类违约升级为 run 失败。准入层提前丢弃违约候选（spec §7.4 校验器
        可拒绝输出、不得注入引用），让诊断收敛到 inconclusive 而非杀 run。
        """
        legal_finding_ids = {
            item.id for item in repository.list_agent_findings(investigation_id)
        } | {item.id for item in batch_findings}
        usable_evidence = {
            item.id
            for item in committed_evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
            and item.runtime_run_id == self.runtime_run_id
        }
        evidence_by_id = {item.id: item for item in committed_evidence}
        admitted: list[RootCauseCandidate] = []
        for draft in drafts:
            # 只有这里把模型 Draft 转成领域实体；模型永远不能填写持久化
            # id/rank，旧的领域对象仅保留给内部测试和已持久化重放路径使用。
            candidate = self._candidate_from_draft(draft)
            finding_refs = {
                *candidate.supporting_finding_ids,
                *candidate.contradicting_finding_ids,
            }
            evidence_refs = {
                *candidate.supporting_evidence_ids,
                *candidate.contradicting_evidence_ids,
            }
            if not finding_refs <= legal_finding_ids:
                code = "candidate draft rejected: candidate_finding_reference"
            elif not evidence_refs <= usable_evidence:
                code = "candidate draft rejected: candidate_evidence_reference"
            elif (
                not candidate.affected_entity
                or not candidate.failure_mechanism
                or not candidate.supporting_evidence_ids
            ):
                code = "candidate draft rejected: candidate_incomplete"
            elif candidate.affected_entity is not None:
                try:
                    _validate_scope_consistency(
                        candidate.affected_entity,
                        candidate.supporting_evidence_ids,
                        evidence_by_id,
                    )
                except V11ResultValidationError as exc:
                    code = f"candidate draft rejected: {exc.code}"
                else:
                    code = ""
            else:
                code = ""
            if not code:
                # 模型候选 ID 只用于它自己的响应，不能成为跨 Investigator
                # 的持久化主键；不同调查员常会同时返回 candidate-1。
                admitted.append(candidate.model_copy(update={"id": self._new_candidate_id()}))
                continue
            self._failures.append("investigator_candidate_draft_rejected")
            audit_executions.append(
                self._failed_execution(
                    task_id=task.id,
                    actor=instance_id,
                    step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                    message=code,
                    analysis_round=task.analysis_round,
                    failure_category=FailureCategory.INVALID_REFERENCE,
                )
            )
        return tuple(admitted)

    @classmethod
    def _candidate_from_draft(
        cls, draft: InvestigatorCandidateDraft | RootCauseCandidate
    ) -> RootCauseCandidate:
        """把模型诊断字段转换为服务端拥有的候选实体。"""
        if isinstance(draft, RootCauseCandidate):
            return draft
        summary = getattr(draft, "summary", "") or (
            f"{draft.affected_entity}: {draft.failure_mechanism}"
        )
        return RootCauseCandidate(
            cause_type=getattr(draft, "cause_type", None),
            affected_entity=draft.affected_entity,
            failure_mechanism=draft.failure_mechanism,
            summary=summary,
            rank=1,
            confidence=getattr(draft, "confidence", 0.5),
            supporting_evidence_ids=list(draft.supporting_evidence_ids),
            contradicting_evidence_ids=list(draft.contradicting_evidence_ids),
            rationale=getattr(draft, "rationale", ""),
            uncertainty=getattr(draft, "uncertainty", ""),
            onset_window_start=getattr(draft, "onset_window_start", None),
            onset_window_end=getattr(draft, "onset_window_end", None),
        )

    def _persist_investigator_result(
        self, repository, investigation_id: str, result: _InvestigatorResult
    ) -> None:
        findings = repository.list_agent_findings(investigation_id)
        executions = repository.list_executions(investigation_id)
        findings_by_id = {item.id: item for item in findings}
        findings_by_id.update({item.id: item for item in result.findings})
        executions_by_id = {item.id: item for item in executions}
        executions_by_id[result.execution.id] = result.execution
        for audit in result.audit_executions:
            executions_by_id[audit.id] = audit
        review = repository.get_coordination_review(investigation_id)
        repository.save_multi_agent_result(
            investigation_id,
            list(findings_by_id.values()),
            list(executions_by_id.values()),
            review,
        )

    def _persist_candidate_projection(
        self,
        repository,
        investigation_id: str,
        new_candidates: Iterable[RootCauseCandidate],
    ) -> None:
        current = repository.get_coordination_review(investigation_id)
        existing = list(current.candidates) if current is not None else []
        by_id = {item.id: item for item in existing}
        for candidate in new_candidates:
            previous = by_id.get(candidate.id)
            if previous is not None and previous != candidate:
                raise V11RuntimeContractError("duplicate candidate has different content")
            by_id[candidate.id] = candidate
        if not by_id:
            return
        # Investigator 的 rank 只在各自响应批次内有意义；并行合并后必须建立
        # 一个稳定的 review 全局序列，否则三个批次都返回 rank=1 会让终态
        # 校验得到重复 rank。这里仅分配持久化投影的展示序号，不选择、改写
        # 或补造任何根因内容；候选顺序由确定的 task/gather 顺序保持。
        normalized_candidates = [
            candidate.model_copy(update={"rank": rank})
            for rank, candidate in enumerate(by_id.values(), start=1)
        ]
        review = (
            current
            if current is not None
            else self._empty_review(repository, investigation_id)
        ).model_copy(update={"candidates": normalized_candidates})
        repository.save_coordination_review(review)

    async def _commit_session(
        self, repository, investigation_id: str, session: AdaptiveToolSession
    ) -> None:
        if self.runtime_run_id is None:
            raise V11RuntimeContractError("tool session lacks runtime owner")
        async with self._commit_lock:
            record = repository.get(investigation_id)
            evidence_by_id = {item.id: item for item in record.evidence}
            for item in session.new_evidence:
                owned = item
                if item.runtime_run_id is None:
                    owned = item.model_copy(
                        update={"runtime_run_id": self.runtime_run_id}
                    )
                previous = evidence_by_id.get(owned.id)
                if previous is not None and previous != owned:
                    raise V11RuntimeContractError(
                        "duplicate evidence has different content"
                    )
                evidence_by_id[owned.id] = owned
            provider_results = {
                item.model_dump_json(): item for item in record.provider_results
            }
            provider_results.update(
                {item.model_dump_json(): item for item in session.provider_results}
            )
            record = record.model_copy(
                update={
                    "evidence": list(evidence_by_id.values()),
                    "provider_results": list(provider_results.values()),
                    "updated_at": datetime.now(UTC),
                }
            )
            repository.save(record)
            existing_calls = {
                item.id: item
                for item in repository.list_tool_calls(investigation_id)
            }
            for call in session.tool_calls:
                owned_call = call
                if call.runtime_run_id is None:
                    owned_call = call.model_copy(
                        update={"runtime_run_id": self.runtime_run_id}
                    )
                existing_calls[owned_call.id] = owned_call
            repository.save_tool_calls(
                investigation_id, list(existing_calls.values())
            )

    def _finding_from_draft(
        self,
        draft: InvestigatorFindingDraft,
        *,
        investigation_id: str,
        task: DiagnosisTask,
        instance_id: str,
        round_number: int,
        assessment: CriticAssessment | None,
        evidence: list[EvidenceItem],
    ) -> AgentFinding:
        usable = {
            item.id
            for item in evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        }
        references = {*draft.evidence_ids, *draft.contradicting_evidence_ids}
        if draft.finding_type == AgentFindingType.GAP:
            # GAP 的语义是“证据缺失”；引用已提交的 failed/skipped 证据正是其
            # 正确出处（spec §7.2 只约束非 gap finding 与未提交输出）。
            committed = {item.id for item in evidence}
            if not references <= committed:
                raise V11RuntimeContractError(
                    "Investigator referenced uncommitted evidence"
                )
        elif not references <= usable:
            raise V11RuntimeContractError("Investigator referenced uncommitted evidence")
        if draft.finding_type != AgentFindingType.GAP and not draft.evidence_ids:
            raise V11RuntimeContractError("non-gap finding requires evidence")
        if draft.finding_type != AgentFindingType.GAP:
            try:
                _validate_scope_consistency(
                    draft.affected_entity,
                    draft.evidence_ids,
                    {item.id: item for item in evidence},
                )
            except V11ResultValidationError as exc:
                # 终态 validator 与 draft 准入必须共享同一 scope 合同；提前
                # 拒绝不一致 draft，避免把可局部丢弃的模型违约升级成整 run 失败。
                raise V11RuntimeContractError(
                    f"finding draft violates finding contract: {exc.code}"
                ) from exc
        try:
            return AgentFinding(
                investigation_id=investigation_id,
                agent_name=FindingActor.INVESTIGATOR,
                agent_instance_id=instance_id,
                finding_type=draft.finding_type,
                summary=draft.summary,
                confidence=draft.confidence,
                evidence_ids=draft.evidence_ids,
                related_cause_type=draft.related_cause_type,
                severity=draft.severity,
                rationale=draft.rationale,
                gaps=draft.gaps,
                blocking=draft.blocking,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                analysis_round=round_number,
                task_id=task.id,
                runtime_run_id=self.runtime_run_id,
                critic_assessment_id=(
                    assessment.id if assessment is not None else None
                ),
                affected_entity=draft.affected_entity,
                failure_mechanism=draft.failure_mechanism,
                contradicting_evidence_ids=draft.contradicting_evidence_ids,
            )
        except ValidationError as exc:
            # 领域规则违约（如 blocking 要求 gap+非空 gaps）与引用违约同级：
            # 包装为契约错误走 per-draft 拒绝，而不是漏网 ValidationError 杀 run。
            # validator 消息是固定字符串（非模型文本），保留以便区分模型违约与
            # 未来调用方 bug；截断兜底防御异常消息形态变化。
            detail = next(
                (
                    str(error["msg"]).removeprefix("Value error, ")
                    for error in exc.errors()
                    if error.get("type") == "value_error"
                ),
                "unknown",
            )
            raise V11RuntimeContractError(
                f"finding draft violates finding contract: {detail[:120]}"
            ) from exc

    def _normalize_assessments(
        self,
        assessments: Iterable[
            CriticAssessmentDraft | CriticCompactAssessmentDraft | CriticAssessment
        ],
        *,
        candidate_ids: set[str],
        runtime_run_id: str,
        review_round: int,
        usable_evidence_ids: set[str] | None = None,
        existing_assessments: Iterable[CriticAssessment] = (),
    ) -> list[CriticAssessment]:
        existing_by_candidate = {
            item.candidate_id: item for item in existing_assessments
        }
        values: list[CriticAssessment] = []
        for draft in assessments:
            if isinstance(draft, CriticAssessment):
                candidate_ref = draft.candidate_id
                values.append(
                    draft.model_copy(
                        update={
                            "runtime_run_id": runtime_run_id,
                            "review_round": review_round,
                        }
                    )
                )
                continue
            candidate_ref = draft.candidate_ref
            if candidate_ref not in candidate_ids:
                raise V11RuntimeContractError(
                    "critic assessment candidate reference is unknown"
                )
            supporting_evidence_ids = list(
                getattr(draft, "supporting_evidence_ids", [])
            )
            contradicting_evidence_ids = list(
                getattr(draft, "contradicting_evidence_ids", [])
            )
            checks = [
                check
                if isinstance(check, CausalCheck)
                else CausalCheck(
                    name=check.name,
                    status=check.status,
                    summary=check.status.value,
                    evidence_ids=list(check.evidence_ids),
                    gap=check.gap,
                )
                for check in draft.checks
            ]
            references = {
                *supporting_evidence_ids,
                *contradicting_evidence_ids,
                *(evidence_id for check in draft.checks for evidence_id in check.evidence_ids),
            }
            if usable_evidence_ids is not None and not references <= usable_evidence_ids:
                raise V11RuntimeContractError(
                    "critic assessment evidence reference is invalid"
                )
            values.append(
                CriticAssessment(
                    id=existing_by_candidate[candidate_ref].id
                    if candidate_ref in existing_by_candidate
                    else f"assessment-{uuid4().hex}",
                    candidate_id=candidate_ref,
                    verdict=draft.verdict,
                    checks=checks,
                    supporting_evidence_ids=supporting_evidence_ids,
                    contradicting_evidence_ids=contradicting_evidence_ids,
                    gap=getattr(draft, "gap", None),
                    supplemental_task_ids=list(
                        getattr(draft, "supplemental_task_ids", [])
                    ),
                    summary=(
                        getattr(draft, "summary", None)
                        or f"{draft.verdict.value} review"
                    ),
                    runtime_run_id=runtime_run_id,
                    review_round=review_round,
                )
            )
        if {item.candidate_id for item in values} != candidate_ids:
            raise V11RuntimeContractError("Critic must assess every candidate exactly once")
        if len(values) != len(candidate_ids):
            raise V11RuntimeContractError("Critic must assess every candidate exactly once")
        if len({item.id for item in values}) != len(values):
            raise V11RuntimeContractError("Critic assessment IDs must be unique")
        if review_round == 2 and any(item.verdict.value == "needs_evidence" for item in values):
            raise V11RuntimeContractError("reconciliation cannot request evidence")
        return values

    def _supplemental_tasks(
        self,
        output: CriticOutput | CriticCompactOutput,
        assessments: list[CriticAssessment],
        *,
        runtime_run_id: str,
    ) -> tuple[list[DiagnosisTask], dict[str, str]]:
        needs = [item for item in assessments if item.verdict.value == "needs_evidence"]
        output_tasks = list(getattr(output, "tasks", []))
        if not needs:
            if output_tasks:
                raise V11RuntimeContractError("only needs_evidence may create round two tasks")
            return [], {}
        if len(output_tasks) > 3:
            raise V11RuntimeContractError("round two task batch is out of bounds")
        task_by_id = {item.id: item for item in output_tasks}
        declared_ids = [
            task_id
            for assessment in needs
            for task_id in assessment.supplemental_task_ids
        ]
        if len(set(declared_ids)) != len(declared_ids):
            raise V11RuntimeContractError("round two task IDs must be unique")
        if set(task_by_id) != set(declared_ids):
            raise V11RuntimeContractError(
                "round two task IDs must match Critic supplemental IDs"
            )
        task_id_by_draft_id = {
            task_id: self._new_task_id() for task_id in declared_ids
        }
        tasks: list[DiagnosisTask] = []
        for assessment in needs:
            ids = list(assessment.supplemental_task_ids)
            if not ids:
                raise V11RuntimeContractError("needs_evidence lacks a round two task")
            for task_id in ids:
                draft = task_by_id.get(task_id)
                if draft is None:
                    raise V11RuntimeContractError(
                        "round two task is not declared by Critic"
                    )
                tasks.append(
                    DiagnosisTask(
                        # Supplemental task ids are also model drafts and must not
                        # become global SQLite primary keys.
                        id=task_id_by_draft_id[draft.id],
                        title=draft.title,
                        description=draft.description,
                        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                        agent_name=ExecutionActor.INVESTIGATOR.value,
                        tool_names=list(self._agent_manifest()),
                        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                        analysis_round=2,
                        strategy=getattr(draft, "strategy", None),
                        evidence_scope=(
                            draft.evidence_scope.model_dump(mode="json")
                            if getattr(draft, "evidence_scope", None) is not None
                            else None
                        ),
                        expected_discriminator=getattr(
                            draft, "expected_discriminator", None
                        ),
                        information_gap=(
                            getattr(draft, "information_gap", None)
                            or assessment.gap
                        ),
                        runtime_run_id=runtime_run_id,
                        critic_assessment_id=assessment.id,
                    )
                )
        if len(tasks) > 3 or len({item.id for item in tasks}) != len(tasks):
            raise V11RuntimeContractError("round two task batch must be unique and bounded")
        return tasks, task_id_by_draft_id

    def _bound_supplemental_work(
        self,
        assessments: list[CriticAssessment],
        tasks: list[DiagnosisTask],
        *,
        remaining_tool_budget: int,
    ) -> tuple[list[CriticAssessment], list[DiagnosisTask]]:
        """在补证批次入库前按剩余额度做确定性准入。"""
        if remaining_tool_budget < 0:
            raise V11RuntimeContractError("remaining tool budget must be non-negative")
        if not tasks:
            return assessments, tasks

        task_ids_by_assessment = {
            assessment.id: tuple(assessment.supplemental_task_ids)
            for assessment in assessments
            if assessment.verdict == CriticVerdict.NEEDS_EVIDENCE
        }
        admitted_task_ids: set[str] = set()
        admitted_count = 0
        bounded: list[CriticAssessment] = []
        degraded = False
        for assessment in assessments:
            if assessment.verdict != CriticVerdict.NEEDS_EVIDENCE:
                bounded.append(assessment)
                continue
            task_ids = task_ids_by_assessment[assessment.id]
            if admitted_count + len(task_ids) <= remaining_tool_budget:
                admitted_task_ids.update(task_ids)
                admitted_count += len(task_ids)
                bounded.append(assessment)
                continue
            degraded = True
            bounded.append(
                assessment.model_copy(
                    update={
                        "verdict": CriticVerdict.INCONCLUSIVE,
                        "gap": None,
                        "supplemental_task_ids": [],
                        "summary": (
                            f"{assessment.summary} Supplemental evidence was not "
                            "scheduled because the remaining tool budget cannot "
                            "execute the requested batch."
                        )[:512],
                    }
                )
            )

        if degraded:
            self._failures.append("supplemental_tool_budget_exhausted")
        return bounded, [task for task in tasks if task.id in admitted_task_ids]

    def _empty_review(self, repository, investigation_id: str) -> CoordinationReview:
        return CoordinationReview(
            investigation_id=investigation_id,
            runtime_run_id=self.runtime_run_id,
            authority_mode=AuthorityMode.AGENT,
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            model_provider=self.model_provider,
            model_name=self.model_name,
        )

    def _record_execution(
        self, repository, investigation_id: str, execution: AgentExecution
    ) -> None:
        if execution.runtime_run_id != self.runtime_run_id:
            raise V11RuntimeContractError("execution owner does not match the run")
        stored = [
            item
            for item in repository.list_executions(investigation_id)
            if item.id != execution.id
        ]
        stored.append(execution)
        repository.save_executions(investigation_id, stored)

    def _failed_execution(
        self,
        *,
        task_id: str,
        actor: str,
        step_kind: ExecutionStepKind,
        message: str,
        analysis_round: int = 1,
        failure_category: FailureCategory = FailureCategory.UNKNOWN,
    ) -> AgentExecution:
        now = datetime.now(UTC)
        return AgentExecution(
            task_id=task_id,
            agent_name=actor,
            runtime_run_id=self.runtime_run_id,
            status=AgentExecutionStatus.FAILED,
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            analysis_round=analysis_round,
            step_kind=step_kind,
            runtime_attempt_id=f"manual-{uuid4().hex}",
            failure_category=failure_category,
            model_provider=self.model_provider,
            model_name=self.model_name,
            error_message=message,
            started_at=now,
            completed_at=now,
            deadline_at=self._deadline_at,
        )

    def _ensure_failed_execution(
        self,
        repository,
        investigation_id: str,
        *,
        task_id: str,
        actor: str,
        step_kind: ExecutionStepKind,
        message: str,
        analysis_round: int = 1,
        failure_category: FailureCategory = FailureCategory.INVALID_OUTPUT,
    ) -> None:
        """保留一次 actor action 的失败审计，避免模型 audit 后重复造记录。"""
        matches = [
            item
            for item in repository.list_executions(investigation_id)
            if item.task_id == task_id and item.step_kind == step_kind
        ]
        existing = (
            max(
                matches,
                key=lambda item: (
                    item.attempt,
                    item.started_at or datetime.min.replace(tzinfo=UTC),
                    item.id,
                ),
            )
            if matches
            else None
        )
        if existing is not None and existing.status == AgentExecutionStatus.FAILED:
            return
        if existing is not None:
            now = datetime.now(UTC)
            self._record_execution(
                repository,
                investigation_id,
                existing.model_copy(
                    update={
                        "status": AgentExecutionStatus.FAILED,
                        "failure_category": failure_category,
                        "error_message": message,
                        "completed_at": now,
                        "deadline_at": existing.deadline_at or self._deadline_at,
                        "runtime_attempt_id": existing.runtime_attempt_id
                        or f"manual-{uuid4().hex}",
                        "duration_ms": max(
                            0,
                            int(
                                (
                                    now
                                    - (existing.started_at or now)
                                ).total_seconds()
                                * 1000
                            ),
                        ),
                    }
                ),
            )
            return
        self._record_execution(
            repository,
            investigation_id,
            self._failed_execution(
                task_id=task_id,
                actor=actor,
                step_kind=step_kind,
                message=message,
                analysis_round=analysis_round,
                failure_category=failure_category,
            ),
        )

    def _terminalize_unexpected_failure(self, repository, investigation_id: str) -> None:
        """Phase 边界异常时清空诊断投影并保留 failed 终态。"""
        reason = "v11 phase persistence or execution failed"
        self._terminal_failure = True
        self._terminal_failure_reason = reason
        self._failures.append(reason)
        try:
            repository.update_status(
                investigation_id,
                InvestigationStatus.FAILED,
                failure_reason=reason,
            )
        except Exception:
            return
        try:
            review = repository.get_coordination_review(investigation_id)
            if review is not None:
                repository.save_coordination_review(
                    review.model_copy(
                        update={
                            "candidates": [],
                            "critic_assessments": [],
                            "lead_decision": None,
                            "diagnostic_status": None,
                            "run_status": MultiAgentRunStatus.FAILED,
                            "stop_reason": "v11_phase_failed",
                            "summary": "V11 phase failed.",
                        }
                    )
                )
        except Exception:
            pass
        try:
            self._update_summary(repository, investigation_id)
        except Exception:
            pass

    def _mark_terminal_failure(
        self,
        repository,
        investigation_id: str,
        reason: str,
        *,
        preserve_candidates: bool = False,
    ) -> None:
        """统一收敛必需 actor 失败，避免用 inconclusive 掩盖运行失败。"""
        self._terminal_failure = True
        self._terminal_failure_reason = reason
        self._failures.append(reason)
        repository.update_status(
            investigation_id,
            InvestigationStatus.FAILED,
            failure_reason=reason,
        )
        review = repository.get_coordination_review(investigation_id)
        if review is not None:
            projection = {
                "lead_decision": None,
                "diagnostic_status": None,
                "run_status": MultiAgentRunStatus.FAILED,
                "stop_reason": (
                    reason.replace(" ", "_")
                    if preserve_candidates
                    else "v11_required_actor_failed"
                ),
                "summary": "V11 required actor failed.",
            }
            if not preserve_candidates:
                projection.update(
                    {"candidates": [], "critic_assessments": [], "root_causes": []}
                )
            repository.save_coordination_review(
                review.model_copy(update=projection)
            )
        self._update_summary(repository, investigation_id)

    def _restore_failure_memory(self, repository, investigation_id: str) -> None:
        """跨进程 resume 后从持久化 failed/cancelled execution 回填失败记忆。

        持久化执行是失败记忆的唯一事实来源；内存 _failures 只是其运行期缓存。
        """
        for item in repository.list_executions(investigation_id):
            if (
                item.runtime_run_id == self.runtime_run_id
                and item.status
                in {AgentExecutionStatus.FAILED, AgentExecutionStatus.CANCELLED}
                and item.id not in self._failures
            ):
                self._failures.append(item.id)

    def _update_summary(self, repository, investigation_id: str) -> None:
        from backend.domain.multi_agent import MultiAgentRunSummary

        review = repository.get_coordination_review(investigation_id)
        calls = repository.list_tool_calls(investigation_id)
        self._restore_failure_memory(repository, investigation_id)
        failures = self._failures
        diagnostic_status = review.diagnostic_status if review is not None else None
        status = (
            MultiAgentRunStatus.FAILED
            if self._terminal_failure
            # 失败记忆只影响已发布的诊断（partial）；inconclusive 是无候选的
            # 证据受限停止，终态矩阵固定映射为 COMPLETED。
            else MultiAgentRunStatus.PARTIAL
            if failures and diagnostic_status != DiagnosticStatus.INCONCLUSIVE
            else MultiAgentRunStatus.COMPLETED
        )
        summary = MultiAgentRunSummary(
            status=status,
            failure_reason=(
                self._terminal_failure_reason
                if self._terminal_failure
                else "V11 partial execution"
                if status == MultiAgentRunStatus.PARTIAL
                else None
            ),
            model_provider=self.model_provider,
            model_name=self.model_name,
            strategy=InvestigationStrategy.ADAPTIVE,
            tool_call_count=len(
                {
                    call.logical_call_id or call.id
                    for call in calls
                    if call.runtime_run_id == self.runtime_run_id
                    and call.status.value != "pending"
                }
            ),
            max_tool_calls_per_specialist=self.max_tool_calls_per_specialist,
            max_total_tool_calls=self.max_total_tool_calls,
            total_input_tokens=self._input_tokens,
            total_output_tokens=self._output_tokens,
            completed_rounds=self._completed_rounds,
            investigator_count=len(
                {
                    item.agent_instance_id
                    for item in repository.list_agent_findings(investigation_id)
                    if item.agent_instance_id is not None
                }
            ),
            diagnostic_status=review.diagnostic_status if review is not None else None,
            authority_mode=AuthorityMode.AGENT,
            runtime_run_id=self.runtime_run_id,
        )
        record = repository.get(investigation_id)
        active_runtime_run_id = record.active_runtime_run_id
        if active_runtime_run_id is None and self._execution_contract is not None:
            # durable V11 phase 的隔离快照在 INTAKE commit 前仍是旧 owner；这里只
            # 补本地快照，权威 owner 切换仍由 PhaseCommit 原子提交。
            active_runtime_run_id = self.runtime_run_id
        record = record.model_copy(
            update={
                "active_runtime_run_id": active_runtime_run_id,
                "multi_agent_run": summary,
                "updated_at": datetime.now(UTC),
            }
        )
        repository.save(record)

    def _remaining_tool_budget_for(self, repository, investigation_id: str) -> int:
        limit = (
            self._phase_tool_budget
            if self._phase_tool_budget is not None
            else self.max_total_tool_calls
        )
        used = len(
            {
                call.logical_call_id or call.id
                for call in repository.list_tool_calls(investigation_id)
                if call.runtime_run_id == self.runtime_run_id
                and call.status.value != "pending"
            }
        )
        return max(0, limit - used)

    def _validate_lead_decision(
        self, decision: LeadDecision, review: CoordinationReview
    ) -> None:
        candidate_ids = {item.id for item in review.candidates}
        if decision.action == LeadAction.CONCLUDE:
            accepted = {
                item.candidate_id
                for item in review.critic_assessments
                if item.verdict.value == "accept"
            }
            if not decision.candidate_ids or not set(decision.candidate_ids) <= accepted:
                raise V11RuntimeContractError("Lead may conclude only accepted candidate IDs")
        elif decision.action == LeadAction.INCONCLUSIVE:
            if decision.candidate_ids or decision.task_ids:
                raise V11RuntimeContractError(
                    "inconclusive Lead decision must stop without candidates"
                )
        elif decision.action in {LeadAction.INVESTIGATE, LeadAction.TEST}:
            raise V11RuntimeContractError("Lead adjudication cannot request another round")
        if not set(decision.candidate_ids) <= candidate_ids:
            raise V11RuntimeContractError("Lead references an unknown candidate")

    def _diagnostic_status(self, decision: LeadDecision) -> DiagnosticStatus:
        if self._terminal_failure:
            return DiagnosticStatus.INCONCLUSIVE
        if decision.action == LeadAction.CONCLUDE and decision.candidate_ids:
            return DiagnosticStatus.PARTIAL if self._failures else DiagnosticStatus.COMPLETE
        return DiagnosticStatus.INCONCLUSIVE

    @staticmethod
    def _inconclusive_decision(reason: str) -> LeadDecision:
        return LeadDecision(
            action=LeadAction.INCONCLUSIVE,
            summary=reason,
            stop_reason="insufficient_evidence",
        )

    def _investigator_prompt(
        self,
        event: IncidentEvent,
        task: DiagnosisTask,
        instance_id: str,
        manifest: tuple[str, ...],
        evidence: list[EvidenceItem],
        own_findings: Iterable[AgentFinding],
        assessment: CriticAssessment | None,
        selected_skills: list[str],
    ) -> str:
        has_committed_evidence = bool(evidence) and self.turn is None
        payload = {
            "role": "general investigator",
            "incident": _event_projection(event),
            "task": _investigator_task_projection(task),
            "agent_instance_id": instance_id,
            "round": task.analysis_round,
            "tool_manifest": list(manifest) if not has_committed_evidence else [],
            "tool_contracts": [
                {
                    "name": name,
                    "description": self.tool_registry.get(name).description,
                }
                for name in manifest
            ]
            if not has_committed_evidence
            else [],
            "skills": (
                [_skill_projection(skill) for skill in self.skills]
                if not has_committed_evidence
                else []
            ),
            "selected_skills": selected_skills if not has_committed_evidence else [],
            "own_committed_evidence": [
                _evidence_projection(item) for item in evidence
            ],
            "own_committed_findings": [
                item.model_dump(mode="json") for item in own_findings
            ],
            "critic_assessment": (
                assessment.model_dump(mode="json") if assessment is not None else None
            ),
            "rule": (
                "Use only the bounded committed evidence shown below. Return "
                "findings as an empty list and at most one minimum candidate "
                "draft. Do not use sibling drafts or invent evidence IDs. Do "
                "not emit candidate IDs, ranks, runtime IDs, review fields, or "
                "finding references; cite only committed usable evidence IDs in "
                "candidate evidence fields. When cited evidence has "
                "scope_entity_ids, affected_entity must exactly match an entity "
                "in every cited evidence scope. Every candidate must include a "
                "non-empty affected_entity, a non-empty failure_mechanism, and "
                "at least one supporting usable evidence ID. Cite every directly "
                "relevant committed evidence ID for the candidate, not just the "
                "first signal. If evidence supports only an observed symptom, "
                "write that symptom and explicitly say the causal mechanism is "
                "unresolved; do not write likely, indicates, leak, pressure, or "
                "causing unless the evidence directly supports that claim. If no "
                "candidate observation is supported, return an empty candidates "
                "list."
                if has_committed_evidence
                else "Do not use sibling drafts or invent evidence IDs. Do not "
                "emit candidate IDs, ranks, runtime IDs, review fields, or "
                "finding references; cite only committed usable evidence IDs in "
                "candidate evidence fields. When cited evidence has "
                "scope_entity_ids, affected_entity must exactly match an entity "
                "in every cited evidence scope. Every candidate must include a "
                "non-empty affected_entity, a non-empty failure_mechanism, and "
                "at least one supporting usable evidence ID. If the causal "
                "mechanism is unresolved but a service-level failure symptom is "
                "supported, state that observed symptom and the unresolved cause "
                "in failure_mechanism; otherwise emit no candidate."
            ),
        }
        return json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)

    def _critic_prompt(
        self,
        repository,
        investigation_id: str,
        event: IncidentEvent,
        review: CoordinationReview,
        *,
        round_number: int,
    ) -> str:
        findings = repository.list_agent_findings(investigation_id)
        evidence = _critic_evidence(review, findings, repository.get(investigation_id).evidence)
        compact_output = self.turn is None and len(review.candidates) <= self.max_investigators
        payload = {
            "role": "critic",
            "incident": _event_projection(event),
            "round": round_number,
            "candidates": [
                {
                    "candidate_ref": item.id,
                    "cause_type": item.cause_type,
                    "affected_entity": item.affected_entity,
                    "failure_mechanism": item.failure_mechanism,
                    "summary": item.summary,
                    "supporting_evidence_ids": item.supporting_evidence_ids,
                    "contradicting_evidence_ids": item.contradicting_evidence_ids,
                    "uncertainty": item.uncertainty,
                }
                for item in review.candidates
            ],
            "findings": [item.model_dump(mode="json") for item in findings],
            "evidence": [_evidence_projection(item) for item in evidence],
            "prior_assessments": [
                {
                    "candidate_ref": item.candidate_id,
                    "verdict": item.verdict,
                    "summary": item.summary,
                }
                for item in review.critic_assessments
            ],
            "rule": (
                "Use candidate_ref exactly as provided. Return exactly one concise "
                "assessment per candidate with verdict accept, reject, or "
                "inconclusive; emit seven named causal checks and only committed "
                "evidence IDs. Do not request needs_evidence or tasks in this "
                "bounded review; use inconclusive when evidence is insufficient. "
                "Accept a symptom-level candidate when its stated observation is "
                "directly supported by committed evidence, even if the deeper "
                "causal mechanism remains unresolved; use inconclusive only when "
                "the candidate's stated observation itself is unsupported. "
                "For every check, pass or fail requires one committed evidence ID "
                "and no gap; unknown requires a short gap. Emit only check name, "
                "status, evidence_ids, and gap; do not emit summaries, top-level "
                "evidence arrays, gap, or supplemental_task_ids. Never return "
                "server assessment IDs or candidate_id."
                if compact_output
                else "Use candidate_ref exactly as provided. Return verdict, seven "
                "named causal checks, and only committed evidence IDs; never "
                "return server assessment IDs or candidate_id. Unknown requires "
                "a named gap."
            ),
        }
        return json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)

    def _critic_context(
        self,
        repository,
        investigation_id: str,
        review: CoordinationReview,
        *,
        round_number: int,
    ) -> dict[str, Any]:
        findings = repository.list_agent_findings(investigation_id)
        evidence = _critic_evidence(review, findings, repository.get(investigation_id).evidence)
        return {
            "round": round_number,
            "candidate_refs": [item.id for item in review.candidates],
            "evidence_ids": [item.id for item in evidence],
            "tools": [],
        }

    def _lead_adjudication_prompt(
        self, repository, review: CoordinationReview, event: IncidentEvent
    ) -> str:
        payload = {
            "role": "lead adjudicator",
            "incident": _event_projection(event),
            "candidates": [item.model_dump(mode="json") for item in review.candidates],
            "critic_assessments": [
                item.model_dump(mode="json") for item in review.critic_assessments
            ],
            "rule": (
                "Conclude only with accepted candidate IDs; otherwise "
                "inconclusive with no candidate IDs."
            ),
        }
        return json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)

    def _lead_adjudication_context(self, repository, review: CoordinationReview) -> dict[str, Any]:
        return {
            "candidate_ids": [item.id for item in review.candidates],
            "accepted_candidate_ids": [
                item.candidate_id
                for item in review.critic_assessments
                if item.verdict.value == "accept"
            ],
            "assessment_ids": [item.id for item in review.critic_assessments],
            "tools": [],
        }

    def _lead_prompt(
        self,
        event: IncidentEvent,
        manifest: tuple[str, ...],
        remaining_tool_budget: int,
    ) -> str:
        payload = {
            "incident": _event_projection(event),
            "tool_manifest": manifest,
            "skills": [_skill_projection(skill) for skill in self.skills],
            "remaining_tool_budget": remaining_tool_budget,
            "remaining_token_budget": self._remaining_token_budget,
            "rule": (
                "Persist one to three bounded general Investigator tasks; "
                "do not conclude during planning. In live mode keep each task "
                "concise and return only id, title, description, "
                "information_gap, and optional expected_discriminator; the "
                "server supplies tool names, analysis round, and persistent IDs. "
                "selected_skills must contain only exact skill identifiers from "
                "the skills list in name@version form, or be empty."
                if self.turn is None
                else "Persist one to three bounded general Investigator tasks; "
                "do not conclude during planning. selected_skills must contain "
                "only exact skill identifiers from the skills list in name@version "
                "form, or be empty."
            ),
        }
        return json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _request_index_from_reservation_id(reservation_id: str) -> int | None:
        marker = ":request-"
        if marker not in reservation_id:
            return None
        suffix = reservation_id.rsplit(marker, 1)[1]
        if not suffix.isdigit():
            return None
        index = int(suffix) - 1
        return index if index >= 0 else None

    @classmethod
    def _load_model_request_history(
        cls, events: Iterable[Any]
    ) -> dict[tuple[str, int], list[_ModelRequestEvent]]:
        history: dict[tuple[str, int], list[_ModelRequestEvent]] = {}
        model_event_types = {"model.started", "model.completed", "model.failed"}
        for event in sorted(events, key=lambda item: getattr(item, "sequence", 0)):
            event_type = str(getattr(event, "event_type", ""))
            if event_type not in model_event_types:
                continue
            payload = getattr(event, "safe_payload", {})
            if not isinstance(payload, dict):
                continue
            logical_call_id = payload.get("logical_call_id")
            reservation_id = payload.get("reservation_id")
            if not isinstance(logical_call_id, str) or not isinstance(
                reservation_id, str
            ):
                continue
            request_index = payload.get("request_index")
            if isinstance(request_index, bool) or not isinstance(request_index, int):
                request_index = cls._request_index_from_reservation_id(reservation_id)
            if request_index is None or request_index < 0:
                continue
            reservation_status = payload.get("reservation_status")
            if not isinstance(reservation_status, str):
                reservation_status = {
                    "model.started": "reserved",
                    "model.completed": "completed",
                    "model.failed": "released",
                }[event_type]
            history.setdefault((logical_call_id, request_index), []).append(
                _ModelRequestEvent(reservation_id, reservation_status)
            )
        return history

    def _record_model_request_event(
        self, status: str, safe_payload: dict[str, Any] | None
    ) -> None:
        if not safe_payload:
            return
        logical_call_id = safe_payload.get("logical_call_id")
        reservation_id = safe_payload.get("reservation_id")
        if not isinstance(logical_call_id, str) or not isinstance(
            reservation_id, str
        ):
            return
        request_index = safe_payload.get("request_index")
        if isinstance(request_index, bool) or not isinstance(request_index, int):
            request_index = self._request_index_from_reservation_id(reservation_id)
        if request_index is None or request_index < 0:
            return
        reservation_status = safe_payload.get("reservation_status")
        if not isinstance(reservation_status, str):
            reservation_status = {
                "started": "reserved",
                "completed": "completed",
                "failed": "released",
            }.get(status)
        self._model_request_history.setdefault(
            (logical_call_id, request_index), []
        ).append(_ModelRequestEvent(reservation_id, reservation_status))

    def _model_request_reservation_id(
        self, logical_call_id: str | None, request_index: int
    ) -> str | None:
        base_id = self._model_reservation_id(logical_call_id, request_index)
        if logical_call_id is None or base_id is None:
            return None
        history = self._model_request_history.get((logical_call_id, request_index), [])
        for event in reversed(history):
            if event.reservation_status not in {"retrying", "deferred"}:
                continue
            reservation = self._model_reservations.get(event.reservation_id)
            if event.reservation_status == "deferred" and reservation is None:
                return event.reservation_id
            if reservation is not None and reservation.status == "retrying":
                return event.reservation_id
            break
        used_ids = {event.reservation_id for event in history}
        if (
            base_id not in used_ids
            and base_id not in self._model_reservations
            and base_id not in self._settled_model_reservations
        ):
            return base_id
        replay_index = 1
        while True:
            candidate = (
                f"{logical_call_id}:replay-{replay_index}:request-{request_index + 1}"
            )
            if (
                candidate not in used_ids
                and candidate not in self._model_reservations
                and candidate not in self._settled_model_reservations
            ):
                return candidate
            replay_index += 1

    @staticmethod
    def _request_index_payload(request_index: int | None) -> dict[str, int]:
        return {"request_index": request_index} if request_index is not None else {}

    @staticmethod
    def _actual_input_payload(actual_input_tokens: int | None) -> dict[str, int]:
        return (
            {"actual_input_tokens": actual_input_tokens}
            if actual_input_tokens is not None
            else {}
        )

    def _calibrated_input_estimate(
        self,
        actor: str,
        raw_estimate: int,
        audit: dict[str, int | str],
    ) -> tuple[int, dict[str, Any]]:
        """按同角色已结算 usage 校准估算，并保留固定安全边际。"""
        estimated_total, actual_total, sample_count = self._input_estimate_calibration.get(
            actor, [0, 0, 0]
        )
        factor_basis_points = _INPUT_ESTIMATE_BASIS_POINTS
        if estimated_total > 0 and actual_total > 0:
            observed_basis_points = (
                actual_total * _INPUT_ESTIMATE_BASIS_POINTS // estimated_total
            )
            factor_basis_points = min(
                _INPUT_ESTIMATE_BASIS_POINTS,
                max(
                    _INPUT_ESTIMATE_CALIBRATION_FLOOR_BASIS_POINTS,
                    (
                        observed_basis_points
                        * _INPUT_ESTIMATE_CALIBRATION_SAFETY_NUMERATOR
                        + _INPUT_ESTIMATE_CALIBRATION_SAFETY_DENOMINATOR
                        - 1
                    )
                    // _INPUT_ESTIMATE_CALIBRATION_SAFETY_DENOMINATOR,
                ),
            )
        calibrated = max(
            1,
            (
                raw_estimate * factor_basis_points
                + _INPUT_ESTIMATE_BASIS_POINTS
                - 1
            )
            // _INPUT_ESTIMATE_BASIS_POINTS,
        )
        calibrated_audit: dict[str, Any] = {
            **audit,
            "raw_estimated_tokens": raw_estimate,
            "estimated_tokens": calibrated,
            "calibration_factor_basis_points": factor_basis_points,
            "calibration_samples": sample_count,
        }
        return calibrated, calibrated_audit

    def _record_input_estimate_calibration(
        self, actor: str, raw_estimate: int, actual_input_tokens: int | None
    ) -> None:
        """只用 provider 返回的正数 input usage 更新角色校准。"""
        if raw_estimate <= 0 or not isinstance(actual_input_tokens, int):
            return
        if actual_input_tokens <= 0:
            return
        bucket = self._input_estimate_calibration.setdefault(actor, [0, 0, 0])
        bucket[0] += raw_estimate
        bucket[1] += actual_input_tokens
        bucket[2] += 1

    async def _defer_later_model_retries(
        self,
        logical_call_id: str,
        request_index: int,
        *,
        execution_id: str | None,
        attempt: int,
        actor: str,
    ) -> None:
        """暂时释放后续 retry 的 allocation，保持其 reservation identity。"""
        candidates: list[tuple[int, str, _ModelReservation]] = []
        for (call_id, later_index), history in self._model_request_history.items():
            if call_id != logical_call_id or later_index <= request_index or not history:
                continue
            latest = history[-1]
            reservation = self._model_reservations.get(latest.reservation_id)
            if (
                latest.reservation_status == "retrying"
                and reservation is not None
                and reservation.status == "retrying"
            ):
                candidates.append((later_index, latest.reservation_id, reservation))
        for later_index, reservation_id, reservation in sorted(candidates):
            released_tokens = reservation.reserved_total
            self._remaining_token_budget += released_tokens
            self._model_reservations.pop(reservation_id, None)
            try:
                await self._emit_model(
                    execution_id or reservation_id,
                    "failed",
                    actor,
                    safe_payload={
                        "logical_call_id": logical_call_id,
                        "reservation_id": reservation_id,
                        "reservation_status": "deferred",
                        "reserved_tokens": released_tokens,
                        "input_estimate": reservation.input_estimate,
                        "attempt": attempt,
                        **_input_estimate_audit(reservation.estimate_audit),
                        **self._model_turn_audit_payload(),
                        **self._request_index_payload(later_index),
                    },
                )
            except Exception:
                self._remaining_token_budget -= released_tokens
                self._model_reservations[reservation_id] = reservation
                raise

    async def _reserve_model_budget(
        self,
        requested_budget: int | None,
        prompt: str,
        context: dict[str, Any],
        *,
        reservation_id: str | None = None,
        logical_call_id: str | None = None,
        execution_id: str | None = None,
        attempt: int = 1,
        actor: str = "CoordinatorAgent",
        request_index: int | None = None,
    ) -> tuple[int | None, int, int]:
        if self._remaining_token_budget is None and requested_budget is None:
            return None, 0, 0
        requested = (
            requested_budget
            if requested_budget is not None
            else self._remaining_token_budget
        )
        assert requested is not None
        raw_input_estimate, raw_estimate_audit = _estimate_model_input(prompt, context)
        input_estimate, estimate_audit = self._calibrated_input_estimate(
            actor,
            raw_input_estimate,
            raw_estimate_audit,
        )
        async with self._token_budget_lock:
            existing = (
                self._model_reservations.get(reservation_id)
                if reservation_id is not None
                else None
            )
            if existing is not None:
                if existing.status == "retrying":
                    await self._reserve_run_model_turn()
                    await self._emit_model(
                        execution_id or logical_call_id or reservation_id,
                        "started",
                        actor,
                        safe_payload={
                            "logical_call_id": logical_call_id,
                            "reservation_id": reservation_id,
                            "reservation_status": "reserved",
                            "reserved_tokens": existing.reserved_total,
                            "input_estimate": existing.input_estimate,
                            "attempt": attempt,
                            **_input_estimate_audit(existing.estimate_audit),
                            **self._model_turn_audit_payload(),
                            **self._request_index_payload(request_index),
                        },
                    )
                    existing.status = "reserved"
                return (
                    existing.output_cap,
                    existing.input_estimate,
                    existing.reserved_total,
                )
            if (
                reservation_id is not None
                and reservation_id in self._settled_model_reservations
            ):
                raise V11RuntimeContractError("model reservation was already settled")
            current = self._remaining_token_budget
            if (
                current is not None
                and current <= input_estimate
                and logical_call_id is not None
                and request_index is not None
            ):
                await self._defer_later_model_retries(
                    logical_call_id,
                    request_index,
                    execution_id=execution_id,
                    attempt=attempt,
                    actor=actor,
                )
                current = self._remaining_token_budget
                if requested_budget is None:
                    requested = current
            if requested_budget is None:
                # Agents SDK 未设置 max_tokens 时，只对并发 Investigator 按
                # 尚未占用的调查槽位均分；串行的 Lead/Critic 必须保留全部
                # 当前预算，否则实际输入稍大于估算值就会被错误拒绝。
                remaining_slots = max(
                    1,
                    self.max_investigators - len(self._model_reservations)
                    if actor == ExecutionActor.INVESTIGATOR.value
                    else 1,
                )
                requested = max(
                    input_estimate + 1,
                    (current + remaining_slots - 1) // remaining_slots
                    if current is not None
                    else requested,
                )
            assert requested is not None
            available = min(
                requested,
                current if current is not None else requested,
            )
            if logical_call_id is not None and request_index is not None:
                previous_caps = [
                    cap
                    for (call_id, index), cap in self._model_request_output_caps.items()
                    if call_id == logical_call_id and index < request_index
                ]
                if previous_caps:
                    available = min(available, input_estimate + min(previous_caps) - 1)
            output_cap = available - input_estimate
            if output_cap <= 0:
                raise V11RuntimeContractError("model token budget exhausted")
            if logical_call_id is not None and request_index is not None:
                self._model_request_output_caps[(logical_call_id, request_index)] = (
                    output_cap
                )
            await self._reserve_run_model_turn()
            if current is not None:
                self._remaining_token_budget = current - available
            else:
                self._remaining_token_budget = 0
            if reservation_id is not None:
                self._model_reservations[reservation_id] = _ModelReservation(
                    output_cap=output_cap,
                    input_estimate=input_estimate,
                    reserved_total=available,
                    estimate_audit=estimate_audit,
                )
                try:
                    await self._emit_model(
                        execution_id or logical_call_id or reservation_id,
                        "started",
                        actor,
                        safe_payload={
                            "logical_call_id": logical_call_id,
                            "reservation_id": reservation_id,
                            "reservation_status": "reserved",
                            "reserved_tokens": available,
                            "input_estimate": input_estimate,
                            "attempt": attempt,
                            **_input_estimate_audit(estimate_audit),
                            **self._model_turn_audit_payload(),
                            **self._request_index_payload(request_index),
                        },
                    )
                except Exception:
                    self._model_reservations.pop(reservation_id, None)
                    if current is not None:
                        self._remaining_token_budget += available
                    raise
        return output_cap, input_estimate, available

    async def _settle_model_budget(
        self,
        reserved_total: int,
        actual_total: int,
        *,
        reservation_id: str | None = None,
        logical_call_id: str | None = None,
        execution_id: str | None = None,
        attempt: int = 1,
        actor: str = "CoordinatorAgent",
        request_index: int | None = None,
        input_tokens: int | None = None,
        actual_input_tokens: int | None = None,
        output_tokens: int = 0,
        reservation_status: str = "completed",
    ) -> None:
        if self._remaining_token_budget is None:
            return
        async with self._token_budget_lock:
            if reservation_id is not None:
                if reservation_id in self._settled_model_reservations:
                    return
                reservation = self._model_reservations.get(reservation_id)
                if reservation is None:
                    if actual_total > reserved_total:
                        raise V11RuntimeContractError(
                            "model response exceeded token budget"
                        )
                    return
                if reserved_total != reservation.reserved_total:
                    raise V11RuntimeContractError("model reservation total mismatch")
                raw_estimate = reservation.estimate_audit.get(
                    "raw_estimated_tokens", reservation.input_estimate
                )
                if (
                    reservation_status == "completed"
                    and isinstance(raw_estimate, int)
                ):
                    self._record_input_estimate_calibration(
                        actor,
                        raw_estimate,
                        actual_input_tokens,
                    )
                if actual_total > reserved_total:
                    reported_input = (
                        actual_input_tokens
                        if actual_input_tokens is not None
                        else input_tokens or 0
                    )
                    self._remaining_token_budget = 0
                    self._model_reservations.pop(reservation_id, None)
                    self._settled_model_reservations.add(reservation_id)
                    persistence = asyncio.ensure_future(
                        self._emit_model(
                            execution_id or logical_call_id or reservation_id,
                            "failed",
                            actor,
                            input_tokens=reported_input,
                            output_tokens=output_tokens,
                            safe_payload={
                                "logical_call_id": logical_call_id,
                                "reservation_id": reservation_id,
                                "reservation_status": "rejected",
                                "reserved_tokens": reservation.reserved_total,
                                "input_estimate": reservation.input_estimate,
                                "actual_input_tokens": reported_input,
                                "budget_overrun_tokens": actual_total - reserved_total,
                                "attempt": attempt,
                                **_input_estimate_audit(reservation.estimate_audit),
                                **self._model_turn_audit_payload(),
                                **self._request_index_payload(request_index),
                            },
                        )
                    )
                    cancelled = False
                    # 终止事件可能已被 RuntimeWriter 接受；必须等落盘后再传播取消，
                    # 否则异常清理会把已拒绝的 reservation 误释放。
                    while not persistence.done():
                        try:
                            await asyncio.shield(persistence)
                        except asyncio.CancelledError:
                            cancelled = True
                    persistence.result()
                    if cancelled:
                        raise asyncio.CancelledError
                    raise V11RuntimeContractError(
                        "model response exceeded token budget"
                    )
                if reservation_status == "retrying":
                    await self._emit_model(
                        execution_id or logical_call_id or reservation_id,
                        "failed",
                        actor,
                        safe_payload={
                            "logical_call_id": logical_call_id,
                            "reservation_id": reservation_id,
                            "reservation_status": "retrying",
                            "reserved_tokens": reservation.reserved_total,
                            "input_estimate": reservation.input_estimate,
                            "attempt": attempt,
                            **_input_estimate_audit(reservation.estimate_audit),
                            **self._model_turn_audit_payload(),
                            **self._request_index_payload(request_index),
                        },
                    )
                    reservation.status = "retrying"
                    return
                if input_tokens is None:
                    input_tokens = actual_total
                await self._emit_model(
                    execution_id or logical_call_id or reservation_id,
                    "completed" if reservation_status == "completed" else "failed",
                    actor,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    safe_payload={
                        "logical_call_id": logical_call_id,
                        "reservation_id": reservation_id,
                        "reservation_status": reservation_status,
                        "reserved_tokens": reservation.reserved_total,
                        "input_estimate": reservation.input_estimate,
                        "attempt": attempt,
                        **self._actual_input_payload(actual_input_tokens),
                        **_input_estimate_audit(reservation.estimate_audit),
                        **self._model_turn_audit_payload(),
                        **self._request_index_payload(request_index),
                    },
                )
                self._remaining_token_budget += reserved_total - actual_total
                self._model_reservations.pop(reservation_id, None)
                self._settled_model_reservations.add(reservation_id)
                return
            if actual_total > reserved_total:
                raise V11RuntimeContractError("model response exceeded token budget")
            self._remaining_token_budget += reserved_total - actual_total

    @staticmethod
    def _model_reservation_id(
        logical_call_id: str | None,
        request_index: int,
    ) -> str | None:
        if logical_call_id is None:
            return None
        return f"{logical_call_id}:request-{request_index + 1}"

    @staticmethod
    def _retry_reservation_status(exc: BaseException, attempt: int) -> str:
        category = retryable_failure_category(exc)
        if attempt < 2 and category in {
            FailureCategory.TRANSPORT,
            FailureCategory.RATE_LIMIT,
        }:
            return "retrying"
        return "released"

    def _model_timeout(self) -> float:
        remaining = max(0.0, float(self._remaining_deadline_seconds()))
        if remaining <= 0:
            raise V11RuntimeContractError("V11 model deadline exhausted")
        return min(self.timeout_seconds, remaining)

    async def _call_model(
        self,
        *,
        actor: str,
        prompt: str,
        output_type: type[BaseModel],
        context: dict[str, Any],
        tools: list[Any],
        remaining_token_budget: int | None,
        remaining_tool_budget: int | None = None,
        repository=None,
        investigation_id: str | None = None,
        task_id: str | None = None,
        step_kind: ExecutionStepKind | None = None,
        analysis_round: int | None = None,
    ) -> _ModelTurn:
        self._check_execution()
        self._validate_bound_execution_contract()
        if remaining_token_budget is not None and remaining_token_budget <= 0:
            raise V11RuntimeContractError("model token budget exhausted")
        if tools and remaining_tool_budget is not None and remaining_tool_budget <= 0:
            raise V11RuntimeContractError("model tool budget exhausted")
        if self._model_turn_budget_enabled:
            if self._remaining_model_turns is None:
                raise V11RuntimeContractError(
                    "V11 durable model turn budget is missing"
                )
            if self._remaining_model_turns <= 0:
                raise V11RuntimeContractError("V11 model turn budget exhausted")
        self._model_timeout()
        model_event_id = f"v11-model-{uuid4().hex}"
        reservation_id = self._model_reservation_id(model_event_id, 0)
        audit_enabled = (
            repository is not None
            and investigation_id is not None
            and task_id is not None
            and step_kind is not None
            and self.runtime_run_id is not None
        )
        previous_execution_id: str | None = None
        current_execution_id: str | None = None
        current_started_at: datetime | None = None
        usage_accumulator = _ModelUsageAccumulator({}, set())
        active_prompt = prompt

        async def persist_attempt(
            *,
            status: AgentExecutionStatus,
            attempt: int,
            started_at: datetime,
            failure_category: FailureCategory = FailureCategory.NONE,
            summary: str | None = None,
            error_message: str | None = None,
            input_tokens: int = 0,
            output_tokens: int = 0,
        ) -> None:
            if not audit_enabled or current_execution_id is None:
                return
            if status != AgentExecutionStatus.RUNNING:
                input_tokens, output_tokens = usage_accumulator.attempt_usage(attempt)
            completed_at = datetime.now(UTC)
            self._record_execution(
                repository,
                investigation_id,
                AgentExecution(
                    id=current_execution_id,
                    task_id=task_id,
                    agent_name=actor,
                    runtime_run_id=self.runtime_run_id,
                    status=status,
                    execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                    analysis_round=analysis_round,
                    step_kind=step_kind,
                    attempt=attempt,
                    runtime_attempt_id=current_execution_id,
                    resume_from_execution_id=previous_execution_id,
                    failure_category=failure_category,
                    model_provider=self.model_provider,
                    model_name=self.model_name,
                    summary=summary,
                    error_message=error_message,
                    started_at=started_at,
                    completed_at=completed_at if status != AgentExecutionStatus.RUNNING else None,
                    deadline_at=self._deadline_at,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=max(
                        0,
                        int((completed_at - started_at).total_seconds() * 1000),
                    )
                    if status != AgentExecutionStatus.RUNNING
                    else 0,
                ),
            )

        await self._emit_agent(actor, "started")
        self._model_usage_accumulators[model_event_id] = usage_accumulator
        try:
            async def invoke_model(attempt: int) -> Any:
                nonlocal active_prompt, current_execution_id, current_started_at
                nonlocal previous_execution_id
                attempt_reservation_id = self._model_request_reservation_id(
                    model_event_id, 0
                ) or reservation_id
                current_execution_id = f"exec-{uuid4().hex}"
                current_started_at = datetime.now(UTC)
                await persist_attempt(
                    status=AgentExecutionStatus.RUNNING,
                    attempt=attempt,
                    started_at=current_started_at,
                )
                output_cap: int | None = None
                input_estimate = reserved_total = 0
                try:
                    timeout_seconds = self._model_timeout()
                    if self.turn is not None:
                        reservation = await self._reserve_model_budget(
                            remaining_token_budget,
                            active_prompt,
                            context,
                            reservation_id=attempt_reservation_id,
                            logical_call_id=model_event_id,
                            execution_id=current_execution_id,
                            attempt=attempt,
                            actor=actor,
                        )
                        output_cap, input_estimate, reserved_total = reservation
                        raw_result = await asyncio.wait_for(
                            _maybe_await(
                                self.turn(
                                    actor=actor,
                                    prompt=active_prompt,
                                    output_type=output_type,
                                    tools=tools,
                                    context=context,
                                    remaining_token_budget=output_cap,
                                    max_output_tokens=output_cap,
                                    remaining_tool_budget=remaining_tool_budget,
                                )
                            ),
                            timeout=timeout_seconds,
                        )
                    else:
                        if self.model is None:
                            raise V11RuntimeUnavailable("V11 model is not configured")
                        sdk_model = (
                            _V11BudgetedModel(
                                self.model,
                                self,
                                logical_call_id=model_event_id,
                                execution_id=current_execution_id,
                                attempt_number=attempt,
                                actor=actor,
                            )
                            if isinstance(self.model, Model)
                            else self.model
                        )
                        agent_kwargs: dict[str, Any] = {
                            "name": actor,
                            "instructions": active_prompt,
                            "model": sdk_model,
                            "tools": tools,
                            "output_type": output_type,
                        }
                        model_settings = ModelSettings(
                            retry=ModelRetrySettings(max_retries=0),
                        )
                        if (
                            isinstance(self.model, OpenAICompatibleChatCompletionsModel)
                            and self.model.structured_output_transport
                            == "strict_output_tool"
                        ):
                            output_tool = _strict_output_tool(output_type)
                            agent_kwargs["tools"] = [*tools, output_tool]
                            agent_kwargs["tool_use_behavior"] = {
                                "stop_at_tool_names": [output_tool.name]
                            }
                            agent_kwargs["reset_tool_choice"] = False
                            model_settings = ModelSettings(
                                tool_choice="required",
                                retry=ModelRetrySettings(max_retries=0),
                            )
                        agent_kwargs["model_settings"] = model_settings
                        agent = Agent(**agent_kwargs)
                        model_provider = (
                            _V11ModelProvider(
                                self,
                                logical_call_id=model_event_id,
                                execution_id=current_execution_id,
                                attempt_number=attempt,
                                actor=actor,
                            )
                            if isinstance(self.model, str)
                            else MultiProvider()
                        )
                        try:
                            sdk_turn_ceiling = self.max_turns
                            if self._model_turn_budget_enabled:
                                assert self._remaining_model_turns is not None
                                sdk_turn_ceiling = self._remaining_model_turns
                            raw_result = await asyncio.wait_for(
                                _run_with_model_lifecycle(
                                    agent,
                                    json.dumps(
                                        context,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                    persist_model_event=None,
                                    max_turns=sdk_turn_ceiling,
                                    run_config=RunConfig(
                                        workflow_name="DiagOps V11 Agent RCA",
                                        tracing_disabled=True,
                                        trace_include_sensitive_data=False,
                                        model_provider=model_provider,
                                    ),
                                ),
                                timeout=timeout_seconds,
                            )
                        finally:
                            if isinstance(model_provider, _V11ModelProvider):
                                await model_provider.aclose()
                    measured = _coerce_turn(raw_result)
                    try:
                        if output_type is not BaseModel:
                            output_type.model_validate(measured.output)
                    except (TypeError, ValueError) as exc:
                        # turn 测试适配器与 Agents SDK 共用同一结构化输出门；
                        # 截断/漂移必须进入 bounded retry，而不是在 phase 外静默
                        # 变成 completed 后再由调用方丢失。
                        raise ClassifiedRetryableError(
                            FailureCategory.INVALID_OUTPUT,
                            audit_code=_structured_validation_signature(exc),
                        ) from exc
                    self._model_timeout()
                    if not usage_accumulator.has_attempt_usage(attempt):
                        fallback = usage_accumulator.record_fallback(
                            attempt,
                            measured.input_tokens,
                            measured.output_tokens,
                        )
                        if fallback is not None:
                            self._input_tokens += fallback[0]
                            self._output_tokens += fallback[1]
                    if self.turn is not None:
                        # 同上：turn 测试适配器也必须以实际 usage 结算。
                        settlement_input_tokens = (
                            measured.input_tokens
                            if measured.input_tokens > 0
                            else input_estimate
                        )
                        await self._settle_model_budget(
                            reserved_total,
                            settlement_input_tokens + measured.output_tokens,
                            reservation_id=attempt_reservation_id,
                            logical_call_id=model_event_id,
                            execution_id=current_execution_id,
                            attempt=attempt,
                            actor=actor,
                            input_tokens=settlement_input_tokens,
                            actual_input_tokens=measured.input_tokens,
                            output_tokens=measured.output_tokens,
                            reservation_status="completed",
                        )
                    await persist_attempt(
                        status=AgentExecutionStatus.COMPLETED,
                        attempt=attempt,
                        started_at=current_started_at,
                        summary="model attempt completed",
                        input_tokens=measured.input_tokens,
                        output_tokens=measured.output_tokens,
                    )
                    return measured
                except asyncio.CancelledError:
                    # 失败请求只结算已预扣的 input estimate；未使用 output cap 退回，
                    # 使 retry coordinator 能在同一 frozen budget 内重新预检。
                    if reserved_total:
                        await self._settle_model_budget(
                            reserved_total,
                            input_estimate,
                            reservation_id=attempt_reservation_id,
                            logical_call_id=model_event_id,
                            execution_id=current_execution_id,
                            attempt=attempt,
                            actor=actor,
                            input_tokens=input_estimate,
                            reservation_status="released",
                        )
                    await persist_attempt(
                        status=AgentExecutionStatus.CANCELLED,
                        attempt=attempt,
                        started_at=current_started_at,
                        failure_category=FailureCategory.CANCELLED,
                        error_message="model attempt cancelled",
                        input_tokens=input_estimate,
                    )
                    previous_execution_id = current_execution_id
                    raise
                except Exception as exc:
                    # 失败请求只结算已预扣的 input estimate；未使用 output cap 退回，
                    # 使 retry coordinator 能在同一 frozen budget 内重新预检。
                    if reserved_total:
                        retry_status = self._retry_reservation_status(exc, attempt)
                        await self._settle_model_budget(
                            reserved_total,
                            input_estimate if retry_status == "released" else 0,
                            reservation_id=attempt_reservation_id,
                            logical_call_id=model_event_id,
                            execution_id=current_execution_id,
                            attempt=attempt,
                            actor=actor,
                            input_tokens=(
                                input_estimate if retry_status == "released" else 0
                            ),
                            reservation_status=retry_status,
                        )
                    category = retryable_failure_category(exc)
                    if category is None:
                        category = (
                            FailureCategory.TIMEOUT
                            if isinstance(exc, TimeoutError)
                            else FailureCategory.UNKNOWN
                        )
                    if category == FailureCategory.INVALID_OUTPUT:
                        active_prompt = (
                            f"{prompt}\n\n"
                            f"{_structured_output_retry_feedback(output_type)}"
                        )
                    await persist_attempt(
                        status=AgentExecutionStatus.FAILED,
                        attempt=attempt,
                        started_at=current_started_at,
                        failure_category=category,
                        error_message=(
                            f"model attempt failed: {exc.audit_code[:256]}"
                            if isinstance(
                                getattr(exc, "audit_code", None), str
                            )
                            and exc.audit_code
                            else "model attempt failed"
                        ),
                        input_tokens=input_estimate,
                    )
                    previous_execution_id = current_execution_id
                    raise _model_retryable_exception(exc) from exc

            async def before_retry(_attempt: int, _category) -> None:
                self._check_execution()
                self._validate_bound_execution_contract()
                self._model_timeout()
                if remaining_token_budget is not None and remaining_token_budget <= 0:
                    raise V11RuntimeContractError("model token budget exhausted")
                if (
                    tools
                    and remaining_tool_budget is not None
                    and remaining_tool_budget <= 0
                ):
                    raise V11RuntimeContractError("model tool budget exhausted")
                if self.tool_registry is not None:
                    self._agent_manifest()

            raw = await RetryCoordinator(
                max_retries=3,
                backoff_base_seconds=5.0,
                backoff_cap_seconds=30.0,
            ).run(
                invoke_model,
                before_retry=before_retry,
            )
            self._hit_fault("model_after_send")
            self._check_execution()
            result = _coerce_turn(raw)
            usage = result.input_tokens + result.output_tokens
            if remaining_token_budget is not None and usage > remaining_token_budget:
                raise V11RuntimeContractError("model response exceeded token budget")
            self._model_usage_accumulators.pop(model_event_id, None)
            await self._emit_agent(actor, "completed")
            return _ModelTurn(
                result.output,
                result.input_tokens,
                result.output_tokens,
                current_execution_id,
            )
        except asyncio.CancelledError:
            self._model_usage_accumulators.pop(model_event_id, None)
            await self._emit_agent(actor, "failed")
            raise
        except Exception:
            self._model_usage_accumulators.pop(model_event_id, None)
            await self._emit_agent(actor, "failed")
            raise

    async def _emit_agent(self, actor: str, status: str) -> None:
        if self._persist_agent_event is None:
            return
        await _maybe_await(self._persist_agent_event(actor, status))

    def _record_model_usage_event(
        self,
        status: str,
        input_tokens: int,
        output_tokens: int,
        safe_payload: dict[str, Any] | None,
    ) -> None:
        logical_call_id = (safe_payload or {}).get("logical_call_id")
        if not isinstance(logical_call_id, str):
            return
        accumulator = self._model_usage_accumulators.get(logical_call_id)
        if accumulator is None:
            return
        delta = accumulator.record_event(
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            safe_payload=safe_payload,
        )
        if delta is not None:
            self._input_tokens += delta[0]
            self._output_tokens += delta[1]

    async def _emit_model(
        self,
        execution_id: str,
        status: str,
        actor: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        safe_payload: dict[str, Any] | None = None,
    ) -> None:
        callback = self._persist_model_event
        if callback is None:
            self._record_model_request_event(status, safe_payload)
            self._record_model_usage_event(
                status, input_tokens, output_tokens, safe_payload
            )
            return
        args = (
            execution_id,
            status,
            input_tokens,
            output_tokens,
            actor,
            safe_payload,
        )
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            for size in (6, 5, 4, 3, 2):
                candidate = args[:size]
                try:
                    signature.bind(*candidate)
                except TypeError:
                    continue
                await _maybe_await(callback(*candidate))
                self._record_model_request_event(status, safe_payload)
                self._record_model_usage_event(
                    status, input_tokens, output_tokens, safe_payload
                )
                return
        await _maybe_await(callback(*args))
        self._record_model_request_event(status, safe_payload)
        self._record_model_usage_event(
            status, input_tokens, output_tokens, safe_payload
        )

    @staticmethod
    def _parse_output(value: Any, output_type: type[BaseModel]) -> BaseModel:
        try:
            return output_type.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise V11RuntimeContractError(
                "invalid V11 model output "
                f"[{_structured_validation_signature(exc)}]"
            ) from exc

    def _cleanup_session(self, session: AdaptiveToolSession) -> None:
        session._stopped_agents.update(session.task_ids)
        self._active_sessions.discard(session)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _coerce_turn(raw: Any) -> _ModelTurn:
    if isinstance(raw, _ModelTurn):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        output, usage = raw
        return _ModelTurn(output, *_usage_values(usage))
    if hasattr(raw, "final_output"):
        return _ModelTurn(raw.final_output, *_usage_from_run_result(raw))
    return _ModelTurn(raw)


def _usage_values(usage: Any) -> tuple[int, int]:
    if isinstance(usage, dict):
        return max(0, int(usage.get("input_tokens", 0))), max(
            0, int(usage.get("output_tokens", 0))
        )
    return (
        max(0, int(getattr(usage, "input_tokens", 0))),
        max(0, int(getattr(usage, "output_tokens", 0))),
    )


def _usage_from_run_result(result: Any) -> tuple[int, int]:
    input_tokens = output_tokens = 0
    for response in getattr(result, "raw_responses", []) or []:
        in_count, out_count = _usage_values(getattr(response, "usage", None))
        input_tokens += in_count
        output_tokens += out_count
    return input_tokens, output_tokens


def _event_projection(event: IncidentEvent) -> dict[str, Any]:
    return {
        "source": event.source.value,
        "service": event.service,
        "environment": event.environment,
        "severity": event.severity.value,
        "title": event.title,
        "description": event.description,
        "started_at": event.started_at.astimezone(UTC).isoformat(),
        "time_window_minutes": event.time_window_minutes,
        "signals": event.signals,
    }


def _evidence_projection(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.provider.value,
        "kind": item.kind.value,
        "status": item.status.value,
        "timestamp": item.timestamp.astimezone(UTC).isoformat(),
        "summary": item.summary,
        "scope_entity_ids": sorted(item.scope.entity_ids) if item.scope else [],
    }


def _investigator_task_projection(task: DiagnosisTask) -> dict[str, Any]:
    """仅向 Investigator 暴露诊断任务语义，不暴露运行时主键。"""
    return {
        "title": task.title,
        "description": task.description,
        "analysis_round": task.analysis_round,
        "tool_names": list(task.tool_names),
        "evidence_scope": task.evidence_scope,
        "information_gap": task.information_gap,
    }


def _select_evidence_digest(
    evidence: Iterable[EvidenceItem],
    *,
    max_per_kind: int = 4,
    max_total: int = 16,
) -> list[EvidenceItem]:
    """为模型提供有界的可引用 evidence 摘要，不改变服务端完整投影。"""
    usable = [
        item
        for item in evidence
        if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
    ]
    groups: dict[str, list[EvidenceItem]] = {}
    for item in usable:
        groups.setdefault(item.kind.value, []).append(item)
    for items in groups.values():
        items.sort(
            key=lambda item: (
                float(item.payload.get("change_score", 0.0))
                if isinstance(item.payload.get("change_score"), (int, float))
                else 0.0,
                item.timestamp,
                item.id,
            ),
            reverse=True,
        )
    def kind_priority(kind: str) -> tuple[float, str]:
        top_score = max(
            (
                float(item.payload.get("change_score", 0.0))
                if isinstance(item.payload.get("change_score"), (int, float))
                else 0.0
            )
            for item in groups[kind]
        )
        return (-top_score, kind)

    selected: list[EvidenceItem] = []
    ordered_kinds = sorted(groups, key=kind_priority)
    for offset in range(max_per_kind):
        for kind in ordered_kinds:
            items = groups[kind]
            if offset < len(items):
                selected.append(items[offset])
                if len(selected) >= max_total:
                    return selected
    return selected


def _evidence_for_task(
    task: DiagnosisTask, evidence: Iterable[EvidenceItem]
) -> list[EvidenceItem]:
    """按已持久化 task scope 过滤证据，同时对不确定项保守放行。

    scope 来自模型草稿，但筛选本身完全由代码执行。没有明确 scope、没有
    EvidenceScope、没有 runtime owner 的初始锚点，以及无法判定冲突的证据都
    保留，避免一次不完整的 scope 草稿静默丢失关键证据；只有证据自身明确
    与 task 的实体或时间范围冲突时才排除。
    """
    items = list(evidence)
    if not task.evidence_scope:
        return items
    try:
        scope = EvidenceScopeDraft.model_validate(task.evidence_scope)
    except (TypeError, ValidationError):
        return items

    selected: list[EvidenceItem] = []
    requested_entities = set(scope.entity_ids)
    for item in items:
        # 初始 evidence 是诊断锚点；无 scope 的证据也无法证明与 task 冲突。
        if item.runtime_run_id is None or item.scope is None:
            selected.append(item)
            continue
        item_entities = set(item.scope.entity_ids)
        if requested_entities and item_entities and not (
            requested_entities & item_entities
        ):
            continue
        observed_at = item.scope.observed_at or item.timestamp
        if scope.start_time is not None and observed_at < scope.start_time:
            continue
        if scope.end_time is not None and observed_at > scope.end_time:
            continue
        selected.append(item)

    # 空结果不代表没有证据，只代表 scope 无法安全命中；回退全量，保持
    # “宁可多发不能漏发”的证据充分性约束。
    return selected or items


def _critic_evidence(
    review: CoordinationReview,
    findings: Iterable[AgentFinding],
    evidence: Iterable[EvidenceItem],
) -> list[EvidenceItem]:
    """只把当前候选链实际引用的证据摘要交给 Critic。

    Evidence 的完整记录仍保存在 repository。Critic 只需要判断候选与
    finding 链的因果充分性；把同一 run 中所有 Investigator 的全部查询
    结果再次注入会让工具结果把每一轮的 prompt 放大数倍，并且不增加可
    归因的证据引用。筛选只读取已持久化引用，不由模型决定，因此不会
    改写候选、finding 或证据内容。
    """
    evidence_items = list(evidence)
    referenced_ids = {
        evidence_id
        for candidate in review.candidates
        for evidence_id in (
            *candidate.supporting_evidence_ids,
            *candidate.contradicting_evidence_ids,
        )
    }
    referenced_ids.update(
        evidence_id
        for finding in findings
        for evidence_id in (
            *finding.evidence_ids,
            *finding.contradicting_evidence_ids,
        )
    )
    referenced_ids.update(
        evidence_id
        for assessment in review.critic_assessments
        for evidence_id in (
            *assessment.supporting_evidence_ids,
            *assessment.contradicting_evidence_ids,
            *(evidence_id for check in assessment.checks for evidence_id in check.evidence_ids),
        )
    )
    if not referenced_ids:
        return evidence_items
    selected = [item for item in evidence_items if item.id in referenced_ids]
    return selected or evidence_items


def _skill_projection(skill: DiagnosticSkill) -> dict[str, Any]:
    return {
        "identifier": f"{skill.name}@{skill.version}",
        "name": skill.name,
        "version": skill.version,
        "when_to_use": skill.when_to_use,
        "required_tools": skill.required_tools,
        "steps": skill.steps,
        "expected_evidence": skill.expected_evidence,
        "stop_conditions": skill.stop_conditions,
    }
