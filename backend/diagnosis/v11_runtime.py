"""V11 Agent authority runtime 的共享边界。

本模块只负责 V11 的 Lead、通用 Investigator、Critic 和最终校验；旧的
``AgentsRcaRuntime`` 仍然只服务 v10_legacy。所有 Provider 调用继续经过
既有 ToolRegistry、幂等记录和 RuntimeWriter 回调。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from agents import Agent, Model, ModelRetrySettings, ModelSettings, RunConfig
from agents.models.interface import ModelProvider as AgentsModelProvider
from agents.models.multi_provider import MultiProvider
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.db.models import InvestigationStatus
from backend.diagnosis.adaptive_tools import (
    AdaptiveToolSession,
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
    OpenAICompatibleChatCompletionsModel,
)
from backend.diagnosis.openai_model import OFFICIAL_OPENAI_BASE_URL
from backend.diagnosis.result_validation import (
    V11ResultValidationError,
    validate_v11_result,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingSeverity,
    AgentFindingType,
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
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
)
from backend.domain.runtime import RuntimePhase, validate_v11_execution_contract
from backend.runtime.concurrency import RunStepGate
from backend.safety.redaction import redact_value
from backend.tools.provider_tools import (
    VerifiedMemoryLookup,
    current_investigation_scope,
)
from backend.tools.registry import ToolRegistry, agent_manifest_hash


class V11RuntimeContractError(ValueError):
    """表示 Agent 输出违反 V11 的机械合同。"""


class V11RuntimeUnavailable(RuntimeError):
    """表示 V11 没有可用的模型调用入口。"""


class LeadTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    analysis_round: int = Field(default=1, ge=1, le=2)
    tool_names: list[str] = Field(default_factory=list, max_length=9)
    strategy: str | None = Field(default=None, max_length=128)
    evidence_scope: dict[str, Any] | None = None
    expected_discriminator: str | None = Field(default=None, max_length=256)
    information_gap: str | None = Field(default=None, max_length=256)


class LeadPlanningOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: LeadDecision
    tasks: list[LeadTaskDraft] = Field(default_factory=list, max_length=3)


class InvestigatorFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_type: AgentFindingType
    summary: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    related_cause_type: Any = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = Field(default="", max_length=512)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    blocking: bool = False
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=256)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class InvestigatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=512)
    findings: list[InvestigatorFindingDraft] = Field(default_factory=list, max_length=8)
    candidates: list[RootCauseCandidate] = Field(default_factory=list, max_length=3)


class CriticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=512)
    assessments: list[CriticAssessment] = Field(default_factory=list, max_length=3)
    tasks: list[LeadTaskDraft] = Field(default_factory=list, max_length=3)


class LeadAdjudicationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: LeadDecision


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    output: Any
    input_tokens: int = 0
    output_tokens: int = 0
    execution_id: str | None = None


@dataclass(slots=True)
class _ModelReservation:
    output_cap: int | None
    input_estimate: int
    reserved_total: int
    status: str = "reserved"


@dataclass(frozen=True, slots=True)
class _ModelRequestEvent:
    reservation_id: str
    reservation_status: str | None


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
        request_index = self._request_index
        self._request_index += 1
        reservation_id = self._runtime._model_request_reservation_id(
            self._logical_call_id,
            request_index,
        )
        reservation = await self._runtime._reserve_model_budget(
            model_settings.max_tokens,
            kwargs.get("system_instructions") or "",
            {"input": kwargs.get("input")},
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
            await self._runtime._settle_model_budget(
                reserved_total,
                max(input_estimate, input_tokens) + output_tokens,
                reservation_id=reservation_id,
                logical_call_id=self._logical_call_id,
                execution_id=self._execution_id,
                attempt=self._attempt_number,
                actor=self._actor,
                request_index=request_index,
                input_tokens=max(input_estimate, input_tokens),
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
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise ValueError("V11 timeout must be between one and 120 seconds")
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
        self._model_reservations: dict[str, _ModelReservation] = {}
        self._settled_model_reservations: set[str] = set()
        self._model_request_history: dict[
            tuple[str, int], list[_ModelRequestEvent]
        ] = {}
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
        runtime._model_reservations = {}
        runtime._settled_model_reservations = set()
        runtime._model_request_history = {}
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
        self._execution_contract = copy.deepcopy(phase_input.execution_contract)
        if getattr(phase_input, "execution_contract_version", None) == "v11":
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
        except Exception:
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
        if remaining_tool_budget < 0 or (
            remaining_token_budget is not None and remaining_token_budget < 0
        ):
            raise V11RuntimeContractError("remaining budget must be non-negative")
        self.runtime_run_id = runtime_run_id
        self._remaining_token_budget = remaining_token_budget
        manifest = self._agent_manifest()
        prompt = self._lead_prompt(event, manifest, remaining_tool_budget)
        turn = await self._call_model(
            actor=ExecutionActor.LEAD.value,
            prompt=prompt,
            output_type=LeadPlanningOutput,
            context={
                "incident": _event_projection(event),
                "tool_manifest": manifest,
                "remaining_tool_budget": remaining_tool_budget,
                "remaining_token_budget": remaining_token_budget,
            },
            tools=[],
            remaining_token_budget=remaining_token_budget,
            remaining_tool_budget=remaining_tool_budget,
            repository=repository,
            investigation_id=investigation_id,
            task_id=f"lead-planning-{runtime_run_id}",
            step_kind=ExecutionStepKind.LEAD_PLANNING,
            analysis_round=1,
        )
        parsed = self._parse_output(turn.output, LeadPlanningOutput)
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
        tasks = [
            DiagnosisTask(
                id=task.id,
                title=task.title,
                description=task.description,
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name=ExecutionActor.INVESTIGATOR.value,
                # Investigator 在每个 instance 上共享同一冻结 manifest。
                tool_names=list(manifest),
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                analysis_round=1,
                strategy=task.strategy,
                evidence_scope=task.evidence_scope,
                expected_discriminator=task.expected_discriminator,
                information_gap=task.information_gap,
                runtime_run_id=runtime_run_id,
            )
            for task in output.tasks
        ]
        decision = output.decision.model_copy(
            update={"task_ids": task_ids, "candidate_ids": []}
        )
        return DiagnosisPlan(
            investigation_id=investigation_id,
            runtime_run_id=runtime_run_id,
            tasks=tasks,
            lead_decision=decision,
        )

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
            if len(manifest) != 9 or len(set(manifest)) != len(manifest):
                raise V11RuntimeContractError(
                    "V11 requires exactly nine frozen Agent tools"
                )
            if manifest != tuple(sorted(manifest)):
                raise V11RuntimeContractError("V11 frozen tool manifest is not ordered")
            if contract.get("tool_manifest_hash") != agent_manifest_hash(manifest):
                raise V11RuntimeContractError("V11 frozen tool manifest hash mismatch")
            skill_identity = contract.get("skill_catalog")
            if skill_identity is not None:
                expected_skill_identity = {
                    "catalog_version": SKILL_CATALOG_VERSION,
                    "catalog_hash": skill_catalog_hash(self.skills),
                    "skill_names": ",".join(
                        f"{skill.name}@{skill.version}" for skill in self.skills
                    ),
                }
                if skill_identity != expected_skill_identity:
                    raise V11RuntimeContractError(
                        "V11 frozen skill catalog identity mismatch"
                    )
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
        limits = contract["limits"]
        if not 1 <= int(limits["max_investigators"]) <= 3:
            raise V11RuntimeContractError("V11 investigator limit is out of bounds")
        if int(limits["max_rounds"]) not in {1, 2}:
            raise V11RuntimeContractError("V11 round limit is out of bounds")
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
                        seed_evidence=base_evidence,
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
            turn = await self._call_model(
                actor=ExecutionActor.CRITIC.value,
                prompt=self._critic_prompt(
                    repository, investigation_id, event, review, round_number=1
                ),
                output_type=CriticOutput,
                context=self._critic_context(
                    repository, investigation_id, review, round_number=1
                ),
                tools=[],
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(
                    repository, investigation_id
                ),
                repository=repository,
                investigation_id=investigation_id,
                task_id=f"critic-review-{self.runtime_run_id}",
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                analysis_round=1,
            )
            output = self._parse_output(turn.output, CriticOutput)
            assessments = self._normalize_assessments(
                output.assessments,
                candidate_ids={item.id for item in review.candidates},
                runtime_run_id=self.runtime_run_id or "",
                review_round=1,
            )
            tasks = self._supplemental_tasks(
                output,
                assessments,
                runtime_run_id=self.runtime_run_id or "",
            )
            existing_tasks = repository.list_tasks(investigation_id)
            repository.save_tasks(investigation_id, [*existing_tasks, *tasks])
            review = review.model_copy(
                update={
                    "critic_assessments": assessments,
                    "summary": output.summary or "Critic review completed.",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            self._ensure_failed_execution(
                repository,
                investigation_id,
                task_id=f"critic-review-{self.runtime_run_id}",
                actor=ExecutionActor.CRITIC.value,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                message="critic review failed",
            )
            review = review.model_copy(
                update={
                    "candidates": [],
                    "critic_assessments": [],
                    "lead_decision": None,
                    "diagnostic_status": None,
                    "run_status": MultiAgentRunStatus.FAILED,
                    "stop_reason": "critic_review_failed",
                    "summary": "Critic review failed.",
                }
            )
            repository.save_coordination_review(review)
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "critic review failed",
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
                        seed_evidence=evidence,
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
            self._ensure_failed_execution(
                repository,
                investigation_id,
                task_id=f"critic-reconciliation-{self.runtime_run_id}",
                actor=ExecutionActor.CRITIC.value,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                message="critic reconciliation failed",
                analysis_round=2,
            )
            review = review.model_copy(
                update={
                    "candidates": [],
                    "critic_assessments": [],
                    "lead_decision": None,
                    "diagnostic_status": None,
                    "run_status": MultiAgentRunStatus.FAILED,
                    "stop_reason": "critic_reconciliation_failed",
                    "summary": "Critic reconciliation failed.",
                }
            )
            repository.save_coordination_review(review)
            self._mark_terminal_failure(
                repository,
                investigation_id,
                "critic reconciliation failed",
            )
            return review
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    async def lead_adjudication(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview:
        """Lead 只可采纳 Critic accepted IDs；无候选时结论必须 inconclusive。"""
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            review = self._empty_review(repository, investigation_id)
        decision: LeadDecision | None = None
        try:
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
            "run_status": (
                MultiAgentRunStatus.PARTIAL
                if self._failures
                else MultiAgentRunStatus.COMPLETED
            ),
            "summary": decision.summary,
        }
        if decision.action == LeadAction.INCONCLUSIVE:
            # Lead 的语义归一化必须清掉候选及其 assessment 投影；validator 只能机械校验。
            projection.update(
                {"candidates": [], "critic_assessments": [], "root_causes": []}
            )
        review = review.model_copy(
            update=projection
        )
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review

    async def result_validation(
        self, *, repository, investigation_id: str, event: IncidentEvent
    ) -> CoordinationReview:
        """机械校验结果，失败时只做一次无工具 Lead correction。"""
        del event
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
                review = review.model_copy(
                    update={
                        "lead_decision": corrected,
                        "diagnostic_status": self._diagnostic_status(corrected),
                        "stop_reason": corrected.stop_reason,
                    }
                )
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
            prompt = self._investigator_prompt(
                event,
                task,
                instance_id,
                manifest,
                seed_evidence,
                own_findings,
                assessment,
            )
            turn = await self._call_model(
                actor=ExecutionActor.INVESTIGATOR.value,
                prompt=prompt,
                output_type=InvestigatorOutput,
                context={
                    "incident": _event_projection(event),
                    "task": task.model_dump(mode="json"),
                    "agent_instance_id": instance_id,
                    "round": round_number,
                    "tool_manifest": manifest,
                    "own_committed_evidence_ids": [item.id for item in seed_evidence],
                    "own_committed_finding_ids": [item.id for item in own_findings],
                    "assessment_id": assessment.id if assessment is not None else None,
                    "remaining_tool_budget": self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                },
                tools=session.tools_for(instance_id, round_number),
                remaining_token_budget=self._remaining_token_budget,
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
            output = self._parse_output(turn.output, InvestigatorOutput)
            findings = tuple(
                self._finding_from_draft(
                    draft,
                    investigation_id=investigation_id,
                    task=task,
                    instance_id=instance_id,
                    round_number=round_number,
                    assessment=assessment,
                    evidence=repository.get(investigation_id).evidence,
                )
                for draft in output.findings
            )
            candidates = tuple(output.candidates) if round_number == 1 else ()
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
                    "summary": output.summary or "Investigator completed.",
                }
            )
            return _InvestigatorResult(findings, candidates, execution)
        except asyncio.CancelledError:
            self._cleanup_session(session)
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            execution = self._failed_execution(
                task_id=task.id,
                actor=instance_id,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                message="investigator failed",
                analysis_round=round_number,
            )
            return _InvestigatorResult((), (), execution)
        finally:
            self._cleanup_session(session)

    def _persist_investigator_result(
        self, repository, investigation_id: str, result: _InvestigatorResult
    ) -> None:
        findings = repository.list_agent_findings(investigation_id)
        executions = repository.list_executions(investigation_id)
        findings_by_id = {item.id: item for item in findings}
        findings_by_id.update({item.id: item for item in result.findings})
        executions_by_id = {item.id: item for item in executions}
        executions_by_id[result.execution.id] = result.execution
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
        review = (
            current
            if current is not None
            else self._empty_review(repository, investigation_id)
        ).model_copy(update={"candidates": list(by_id.values())})
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
        available = {
            item.id
            for item in evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        }
        references = [*draft.evidence_ids, *draft.contradicting_evidence_ids]
        if not set(references) <= available:
            raise V11RuntimeContractError("Investigator referenced uncommitted evidence")
        if draft.finding_type != AgentFindingType.GAP and not draft.evidence_ids:
            raise V11RuntimeContractError("non-gap finding requires evidence")
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

    def _normalize_assessments(
        self,
        assessments: Iterable[CriticAssessment],
        *,
        candidate_ids: set[str],
        runtime_run_id: str,
        review_round: int,
    ) -> list[CriticAssessment]:
        values = [
            item.model_copy(
                update={"runtime_run_id": runtime_run_id, "review_round": review_round}
            )
            for item in assessments
        ]
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
        output: CriticOutput,
        assessments: list[CriticAssessment],
        *,
        runtime_run_id: str,
    ) -> list[DiagnosisTask]:
        needs = [item for item in assessments if item.verdict.value == "needs_evidence"]
        if not needs:
            if output.tasks:
                raise V11RuntimeContractError("only needs_evidence may create round two tasks")
            return []
        if len(output.tasks) > 3:
            raise V11RuntimeContractError("round two task batch is out of bounds")
        task_by_id = {item.id: item for item in output.tasks}
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
                        id=draft.id,
                        title=draft.title,
                        description=draft.description,
                        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                        agent_name=ExecutionActor.INVESTIGATOR.value,
                        tool_names=list(self._agent_manifest()),
                        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                        analysis_round=2,
                        strategy=draft.strategy,
                        evidence_scope=draft.evidence_scope,
                        expected_discriminator=draft.expected_discriminator,
                        information_gap=draft.information_gap or assessment.gap,
                        runtime_run_id=runtime_run_id,
                        critic_assessment_id=assessment.id,
                    )
                )
        if len(tasks) > 3 or len({item.id for item in tasks}) != len(tasks):
            raise V11RuntimeContractError("round two task batch must be unique and bounded")
        return tasks

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
            failure_category=FailureCategory.UNKNOWN,
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
                        "failure_category": FailureCategory.INVALID_OUTPUT,
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
            repository.save_coordination_review(
                review.model_copy(
                    update={
                        "candidates": [],
                        "critic_assessments": [],
                        "lead_decision": None,
                        "diagnostic_status": None,
                        "run_status": MultiAgentRunStatus.FAILED,
                        "stop_reason": "v11_required_actor_failed",
                        "summary": "V11 required actor failed.",
                    }
                )
            )
        self._update_summary(repository, investigation_id)

    def _update_summary(self, repository, investigation_id: str) -> None:
        from backend.domain.multi_agent import MultiAgentRunSummary

        review = repository.get_coordination_review(investigation_id)
        executions = repository.list_executions(investigation_id)
        calls = repository.list_tool_calls(investigation_id)
        failures = self._failures or [
            item.id
            for item in executions
            if item.runtime_run_id == self.runtime_run_id
            and item.status in {AgentExecutionStatus.FAILED, AgentExecutionStatus.CANCELLED}
        ]
        summary = MultiAgentRunSummary(
            status=(
                MultiAgentRunStatus.FAILED
                if self._terminal_failure
                else MultiAgentRunStatus.PARTIAL
                if failures
                else MultiAgentRunStatus.COMPLETED
            ),
            failure_reason=(
                self._terminal_failure_reason
                if self._terminal_failure
                else "V11 partial execution"
                if failures
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
            max_total_tool_calls=(
                self._phase_tool_budget
                if self._phase_tool_budget is not None
                else self.max_total_tool_calls
            ),
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
        record = repository.get(investigation_id).model_copy(
            update={"multi_agent_run": summary, "updated_at": datetime.now(UTC)}
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
    ) -> str:
        payload = {
            "role": "general investigator",
            "incident": _event_projection(event),
            "task": task.model_dump(mode="json"),
            "agent_instance_id": instance_id,
            "round": task.analysis_round,
            "tool_manifest": manifest,
            "skills": [_skill_projection(skill) for skill in self.skills],
            "own_committed_evidence": [
                _evidence_projection(item) for item in evidence
            ],
            "own_committed_findings": [
                item.model_dump(mode="json") for item in own_findings
            ],
            "critic_assessment": (
                assessment.model_dump(mode="json") if assessment is not None else None
            ),
            "rule": "Do not use sibling drafts or invent evidence IDs.",
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
        payload = {
            "role": "critic",
            "incident": _event_projection(event),
            "round": round_number,
            "candidates": [item.model_dump(mode="json") for item in review.candidates],
            "findings": [
                item.model_dump(mode="json")
                for item in repository.list_agent_findings(investigation_id)
            ],
            "evidence": [
                _evidence_projection(item)
                for item in repository.get(investigation_id).evidence
            ],
            "prior_assessments": [
                item.model_dump(mode="json") for item in review.critic_assessments
            ],
            "rule": "Exactly seven named causal checks; unknown requires a named gap.",
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
        evidence_ids = [
            item.id
            for item in repository.get(investigation_id).evidence
        ]
        return {
            "round": round_number,
            "candidate_ids": [item.id for item in review.candidates],
            "assessment_ids": [item.id for item in review.critic_assessments],
            "evidence_ids": evidence_ids,
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
                "do not conclude during planning."
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
        input_estimate = max(
            1,
            (
                len(prompt)
                + len(json.dumps(context, ensure_ascii=False, sort_keys=True))
                + 3
            )
            // 4,
        )
        async with self._token_budget_lock:
            existing = (
                self._model_reservations.get(reservation_id)
                if reservation_id is not None
                else None
            )
            if existing is not None:
                if existing.status == "retrying":
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
            assert requested is not None
            available = min(
                requested,
                current if current is not None else requested,
            )
            output_cap = available - input_estimate
            if output_cap <= 0:
                raise V11RuntimeContractError("model token budget exhausted")
            if current is not None:
                self._remaining_token_budget = current - available
            else:
                self._remaining_token_budget = 0
            if reservation_id is not None:
                self._model_reservations[reservation_id] = _ModelReservation(
                    output_cap=output_cap,
                    input_estimate=input_estimate,
                    reserved_total=available,
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
        output_tokens: int = 0,
        reservation_status: str = "completed",
    ) -> None:
        if self._remaining_token_budget is None:
            return
        if actual_total > reserved_total:
            raise V11RuntimeContractError("model response exceeded token budget")
        async with self._token_budget_lock:
            if reservation_id is not None:
                if reservation_id in self._settled_model_reservations:
                    return
                reservation = self._model_reservations.get(reservation_id)
                if reservation is None:
                    return
                if reserved_total != reservation.reserved_total:
                    raise V11RuntimeContractError("model reservation total mismatch")
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
                        **self._request_index_payload(request_index),
                    },
                )
                self._remaining_token_budget += reserved_total - actual_total
                self._model_reservations.pop(reservation_id, None)
                self._settled_model_reservations.add(reservation_id)
                return
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
        if remaining_tool_budget is not None and remaining_tool_budget <= 0:
            raise V11RuntimeContractError("model tool budget exhausted")
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
        try:
            async def invoke_model(attempt: int) -> Any:
                nonlocal current_execution_id, current_started_at, previous_execution_id
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
                            prompt,
                            context,
                            reservation_id=reservation_id,
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
                                    prompt=prompt,
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
                            "instructions": prompt,
                            "model": sdk_model,
                            "tools": tools,
                            "output_type": output_type,
                        }
                        agent_kwargs["model_settings"] = ModelSettings(
                            retry=ModelRetrySettings(max_retries=0),
                        )
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
                            raw_result = await asyncio.wait_for(
                                _run_with_model_lifecycle(
                                    agent,
                                    json.dumps(
                                        context,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                    persist_model_event=None,
                                    max_turns=self.max_turns,
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
                    self._model_timeout()
                    if self.turn is not None:
                        await self._settle_model_budget(
                            reserved_total,
                            measured.input_tokens + measured.output_tokens,
                            reservation_id=reservation_id,
                            logical_call_id=model_event_id,
                            execution_id=current_execution_id,
                            attempt=attempt,
                            actor=actor,
                            input_tokens=measured.input_tokens,
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
                            reservation_id=reservation_id,
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
                            reservation_id=reservation_id,
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
                    await persist_attempt(
                        status=AgentExecutionStatus.FAILED,
                        attempt=attempt,
                        started_at=current_started_at,
                        failure_category=category,
                        error_message="model attempt failed",
                        input_tokens=input_estimate,
                    )
                    previous_execution_id = current_execution_id
                    raise

            async def before_retry(_attempt: int, _category) -> None:
                self._check_execution()
                self._validate_bound_execution_contract()
                self._model_timeout()
                if remaining_token_budget is not None and remaining_token_budget <= 0:
                    raise V11RuntimeContractError("model token budget exhausted")
                if remaining_tool_budget is not None and remaining_tool_budget <= 0:
                    raise V11RuntimeContractError("model tool budget exhausted")
                if self.tool_registry is not None:
                    self._agent_manifest()

            raw = await RetryCoordinator(max_retries=1).run(
                invoke_model,
                before_retry=before_retry,
            )
            self._hit_fault("model_after_send")
            self._check_execution()
            result = _coerce_turn(raw)
            usage = result.input_tokens + result.output_tokens
            if remaining_token_budget is not None and usage > remaining_token_budget:
                raise V11RuntimeContractError("model response exceeded token budget")
            self._input_tokens += result.input_tokens
            self._output_tokens += result.output_tokens
            await self._emit_agent(actor, "completed")
            return _ModelTurn(
                result.output,
                result.input_tokens,
                result.output_tokens,
                current_execution_id,
            )
        except asyncio.CancelledError:
            await self._emit_agent(actor, "failed")
            raise
        except Exception:
            await self._emit_agent(actor, "failed")
            raise

    async def _emit_agent(self, actor: str, status: str) -> None:
        if self._persist_agent_event is None:
            return
        await _maybe_await(self._persist_agent_event(actor, status))

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
                return
        await _maybe_await(callback(*args))
        self._record_model_request_event(status, safe_payload)

    @staticmethod
    def _parse_output(value: Any, output_type: type[BaseModel]) -> BaseModel:
        try:
            return output_type.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise V11RuntimeContractError("invalid V11 model output") from exc

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
    }


def _skill_projection(skill: DiagnosticSkill) -> dict[str, Any]:
    return {
        "name": skill.name,
        "version": skill.version,
        "when_to_use": skill.when_to_use,
        "required_tools": skill.required_tools,
        "steps": skill.steps,
        "expected_evidence": skill.expected_evidence,
        "stop_conditions": skill.stop_conditions,
    }
