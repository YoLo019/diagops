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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agents import Agent, RunConfig
from pydantic import BaseModel, ConfigDict, Field

from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.agents_runtime import _run_with_model_lifecycle
from backend.diagnosis.diagnostic_skills import (
    DIAGNOSTIC_SKILLS,
    DiagnosticSkill,
    validate_skill_catalog,
)
from backend.diagnosis.result_validation import (
    V11ResultValidationError,
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
from backend.domain.runtime import RuntimePhase
from backend.runtime.concurrency import RunStepGate
from backend.safety.redaction import redact_value
from backend.tools.provider_tools import (
    VerifiedMemoryLookup,
    current_investigation_scope,
)
from backend.tools.registry import ToolRegistry


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
        self.tool_timeout_seconds = tool_timeout_seconds
        self.skills = skills
        self.runtime_run_id: str | None = None
        self._remaining_token_budget = token_budget
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
    ) -> V11Runtime:
        """为 durable Run 复制模型边界和预算，不共享运行期状态。"""
        runtime = copy.copy(self)
        runtime.runtime_run_id = runtime_run_id
        runtime.model_provider = ModelProvider(model_provider or self.model_provider)
        runtime.model_name = model_name or self.model_name
        runtime._remaining_token_budget = token_budget
        runtime.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        runtime._phase_tool_budget = None
        runtime._check_execution = lambda: None
        runtime._persist_tool_start = None
        runtime._persist_tool_result = None
        runtime._resolve_tool_result = None
        runtime._persist_agent_event = None
        runtime._persist_model_event = None
        runtime._parallel_limit = RunStepGate(self.max_investigators)
        runtime._active_sessions = set()
        runtime._failures = []
        runtime._input_tokens = 0
        runtime._output_tokens = 0
        runtime._completed_rounds = 0
        return runtime

    def bind_phase(self, phase_input: Any) -> None:
        """绑定当前 phase 的 Runtime fence、写入回调与冻结预算。"""
        self.runtime_run_id = phase_input.run_id
        self._check_execution = phase_input.check_execution or (lambda: None)
        self._resolve_tool_result = phase_input.resolve_tool_result
        self._persist_tool_start = phase_input.persist_tool_start
        self._persist_tool_result = phase_input.persist_tool_result
        self._persist_agent_event = phase_input.persist_agent_event
        self._persist_model_event = phase_input.persist_model_event
        self._hit_fault = phase_input.hit_fault or (lambda _point: None)
        self._phase_tool_budget = phase_input.tool_budget
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
        with current_investigation_scope(investigation_id):
            if phase == RuntimePhase.LEAD_PLANNING:
                return await self.plan_lead(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                    runtime_run_id=run_id,
                    remaining_tool_budget=self._remaining_tool_budget_for(repository),
                    remaining_token_budget=self._remaining_token_budget,
                )
            if phase == RuntimePhase.INVESTIGATOR_ROUND_1:
                return await self.investigator_round_1(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                )
            if phase == RuntimePhase.CRITIC_REVIEW:
                return await self.critic_review(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                )
            if phase == RuntimePhase.INVESTIGATOR_ROUND_2:
                return await self.investigator_round_2(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                )
            if phase == RuntimePhase.CRITIC_RECONCILIATION:
                return await self.critic_reconciliation(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                )
            if phase == RuntimePhase.LEAD_ADJUDICATION:
                return await self.lead_adjudication(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
                )
            if phase == RuntimePhase.RESULT_VALIDATION:
                return await self.result_validation(
                    repository=repository,
                    investigation_id=investigation_id,
                    event=current,
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
        self._record_execution(
            repository,
            AgentExecution(
                task_id=f"lead-planning-{runtime_run_id}",
                agent_name=ExecutionActor.LEAD.value,
                runtime_run_id=runtime_run_id,
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.LEAD_PLANNING,
                analysis_round=1,
                model_provider=self.model_provider,
                model_name=self.model_name,
                summary=parsed.decision.summary,
            ),
        )
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
        manifest = self.tool_registry.agent_manifest()
        if len(manifest) != 9:
            raise V11RuntimeContractError("V11 requires exactly nine Agent tools")
        return manifest

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
        results: list[_InvestigatorResult] = []
        for task in tasks[: self.max_investigators]:
            self._check_execution()
            try:
                result = await self._run_investigator(
                    repository=repository,
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
                continue
            results.append(result)
            candidates.extend(result.candidates)
            self._persist_investigator_result(repository, investigation_id, result)
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
                prompt=self._critic_prompt(repository, event, review, round_number=1),
                output_type=CriticOutput,
                context=self._critic_context(repository, review, round_number=1),
                tools=[],
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(repository),
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
            review = review.model_copy(
                update={
                    "critic_assessments": self._fallback_assessments(review),
                    "summary": "Critic review failed.",
                }
            )
            self._record_execution(
                repository,
                self._failed_execution(
                    task_id=f"critic-review-{self.runtime_run_id}",
                    actor=ExecutionActor.CRITIC.value,
                    step_kind=ExecutionStepKind.CRITIC_REVIEW,
                    message="critic review failed",
                ),
            )
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
        for task in tasks[:3]:
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
            try:
                result = await self._run_investigator(
                    repository=repository,
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
                continue
            findings.extend(result.findings)
            self._persist_investigator_result(repository, investigation_id, result)
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
                prompt=self._critic_prompt(repository, event, review, round_number=2),
                output_type=CriticOutput,
                context=self._critic_context(repository, review, round_number=2),
                tools=[],
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(repository),
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
                remaining_tool_budget=self._remaining_tool_budget_for(repository),
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
                    remaining_tool_budget=self._remaining_tool_budget_for(repository),
                )
                decision = self._parse_output(
                    turn.output, LeadAdjudicationOutput
                ).decision
                self._validate_lead_decision(decision, review)
            except asyncio.CancelledError:
                raise
            except Exception as second_error:
                self._failures.append(type(second_error).__name__)
        if decision is None:
            decision = self._inconclusive_decision("Lead could not establish a valid conclusion.")
        status = self._diagnostic_status(decision)
        review = review.model_copy(
            update={
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
        )
        self._record_execution(
            repository,
            AgentExecution(
                task_id=f"lead-adjudication-{self.runtime_run_id}",
                agent_name=ExecutionActor.LEAD.value,
                runtime_run_id=self.runtime_run_id,
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                analysis_round=2 if self._completed_rounds == 2 else 1,
                model_provider=self.model_provider,
                model_name=self.model_name,
                summary=decision.summary,
            ),
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
                    remaining_tool_budget=self._remaining_tool_budget_for(repository),
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
                review = review.model_copy(
                    update={
                        "lead_decision": self._inconclusive_decision(
                            "V11 result contract could not be repaired."
                        ),
                        "diagnostic_status": DiagnosticStatus.INCONCLUSIVE,
                        "stop_reason": "result_validation_failed",
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
                    task_id_investigation(repository, task.id)
                )
                if item.task_id == task.id and item.analysis_round == round_number
            ),
            None,
        )
        # 上面按 task 反查在不同 repository 实现中不可用时由调用方的任务记录兜底。
        if existing is not None:
            execution = next(
                (
                    item
                    for item in repository.list_executions(
                        task_id_investigation(repository, task.id)
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
                    task_id_investigation(repository, task.id)
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
            max_tool_calls_per_specialist=3,
            max_total_tool_calls=max(
                1, self._remaining_tool_budget_for(repository)
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
                    "remaining_tool_budget": self._remaining_tool_budget_for(repository),
                },
                tools=session.tools_for(instance_id, round_number),
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(repository),
            )
            # 任何 finding 引用前，先收口 tool/evidence 的 durable 投影。
            self._commit_session(repository, session)
            output = self._parse_output(turn.output, InvestigatorOutput)
            findings = tuple(
                self._finding_from_draft(
                    draft,
                    investigation_id=task_id_investigation(repository, task.id),
                    task=task,
                    instance_id=instance_id,
                    round_number=round_number,
                    assessment=assessment,
                    evidence=repository.get(task_id_investigation(repository, task.id)).evidence,
                )
                for draft in output.findings
            )
            candidates = tuple(output.candidates) if round_number == 1 else ()
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
                tool_call_ids=[call.id for call in session.tool_calls],
                evidence_ids=sorted(session.evidence_ids_for(instance_id, round_number)),
                summary=output.summary or "Investigator completed.",
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

    def _commit_session(self, repository, session: AdaptiveToolSession) -> None:
        if self.runtime_run_id is None:
            raise V11RuntimeContractError("tool session lacks runtime owner")
        investigation_id = _session_investigation_id(repository)
        record = repository.get(investigation_id)
        evidence_by_id = {item.id: item for item in record.evidence}
        for item in session.new_evidence:
            owned = item
            if item.runtime_run_id is None:
                owned = item.model_copy(update={"runtime_run_id": self.runtime_run_id})
            previous = evidence_by_id.get(owned.id)
            if previous is not None and previous != owned:
                raise V11RuntimeContractError("duplicate evidence has different content")
            evidence_by_id[owned.id] = owned
        provider_results = {item.model_dump_json(): item for item in record.provider_results}
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
        existing_calls = {item.id: item for item in repository.list_tool_calls(investigation_id)}
        for call in session.tool_calls:
            owned_call = call
            if call.runtime_run_id is None:
                owned_call = call.model_copy(update={"runtime_run_id": self.runtime_run_id})
            existing_calls[owned_call.id] = owned_call
        repository.save_tool_calls(investigation_id, list(existing_calls.values()))

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

    def _fallback_assessments(
        self, review: CoordinationReview
    ) -> list[CriticAssessment]:
        """模型失败时仍保留完整七项 Critic 记录，Lead 只能判 inconclusive。"""
        return [
            CriticAssessment(
                candidate_id=candidate.id,
                verdict=CriticVerdict.INCONCLUSIVE,
                checks=[
                    CausalCheck(
                        name=name,
                        status=CausalCheckStatus.UNKNOWN,
                        summary="Critic output unavailable.",
                        gap="critic output unavailable",
                    )
                    for name in CausalCheckName
                ],
                summary="Critic output unavailable.",
                runtime_run_id=self.runtime_run_id,
                review_round=1,
            )
            for candidate in review.candidates
        ]

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
        tasks: list[DiagnosisTask] = []
        for index, assessment in enumerate(needs):
            ids = list(assessment.supplemental_task_ids)
            if not ids:
                if index >= len(output.tasks):
                    raise V11RuntimeContractError("needs_evidence lacks a round two task")
                ids = [output.tasks[index].id]
            for task_id in ids:
                draft = task_by_id.get(task_id)
                if draft is None:
                    draft = LeadTaskDraft(
                        id=task_id,
                        title="Collect Critic-requested evidence",
                        description=assessment.gap or "Close the named evidence gap.",
                        evidence_scope={"entity_ids": []},
                        information_gap=assessment.gap,
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

    def _record_execution(self, repository, execution: AgentExecution) -> None:
        investigation_id = _session_investigation_id(repository)
        existing = {
            item.id for item in repository.list_executions(investigation_id)
        }
        stored = repository.list_executions(investigation_id)
        if execution.id not in existing:
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
        return AgentExecution(
            task_id=task_id,
            agent_name=actor,
            runtime_run_id=self.runtime_run_id,
            status=AgentExecutionStatus.FAILED,
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            analysis_round=analysis_round,
            step_kind=step_kind,
            failure_category=FailureCategory.UNKNOWN,
            model_provider=self.model_provider,
            model_name=self.model_name,
            error_message=message,
        )

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
            status=(MultiAgentRunStatus.PARTIAL if failures else MultiAgentRunStatus.COMPLETED),
            failure_reason="V11 partial execution" if failures else None,
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
            max_tool_calls_per_specialist=3,
            max_total_tool_calls=self._phase_tool_budget or self.max_total_tool_calls,
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

    def _remaining_tool_budget_for(self, repository) -> int:
        limit = self._phase_tool_budget or self.max_total_tool_calls
        used = len(
            {
                call.logical_call_id or call.id
                for call in repository.list_tool_calls(_session_investigation_id(repository))
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
                for item in repository.list_agent_findings(_session_investigation_id(repository))
            ],
            "evidence": [
                _evidence_projection(item)
                for item in repository.get(_session_investigation_id(repository)).evidence
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
        review: CoordinationReview,
        *,
        round_number: int,
    ) -> dict[str, Any]:
        evidence_ids = [
            item.id
            for item in repository.get(_session_investigation_id(repository)).evidence
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
    ) -> _ModelTurn:
        self._check_execution()
        if remaining_token_budget is not None and remaining_token_budget <= 0:
            raise V11RuntimeContractError("model token budget exhausted")
        execution_id = f"v11-model-{uuid4().hex}"
        manual_lifecycle = self.turn is not None
        await self._emit_agent(actor, "started")
        if manual_lifecycle:
            await self._emit_model(execution_id, "started", actor)
        try:
            if self.turn is not None:
                raw = await asyncio.wait_for(
                    _maybe_await(
                        self.turn(
                            actor=actor,
                            prompt=prompt,
                            output_type=output_type,
                            tools=tools,
                            context=context,
                            remaining_token_budget=remaining_token_budget,
                            max_output_tokens=remaining_token_budget,
                            remaining_tool_budget=remaining_tool_budget,
                        )
                    ),
                    timeout=self.timeout_seconds,
                )
            else:
                if self.model is None:
                    raise V11RuntimeUnavailable("V11 model is not configured")
                agent = Agent(
                    name=actor,
                    instructions=prompt,
                    model=self.model,
                    tools=tools,
                    output_type=output_type,
                )
                raw = await asyncio.wait_for(
                    _run_with_model_lifecycle(
                        agent,
                        json.dumps(context, ensure_ascii=False, sort_keys=True),
                        persist_model_event=self._persist_model_event,
                        max_turns=self.max_turns,
                        run_config=RunConfig(
                            workflow_name="DiagOps V11 Agent RCA",
                            tracing_disabled=True,
                            trace_include_sensitive_data=False,
                        ),
                    ),
                    timeout=self.timeout_seconds,
                )
            self._hit_fault("model_after_send")
            self._check_execution()
            result = _coerce_turn(raw)
            usage = result.input_tokens + result.output_tokens
            if remaining_token_budget is not None and usage > remaining_token_budget:
                raise V11RuntimeContractError("model response exceeded token budget")
            if self._remaining_token_budget is not None:
                if usage > self._remaining_token_budget:
                    raise V11RuntimeContractError("model response exceeded token budget")
                self._remaining_token_budget -= usage
            self._input_tokens += result.input_tokens
            self._output_tokens += result.output_tokens
            if manual_lifecycle:
                await self._emit_model(
                    execution_id,
                    "completed",
                    actor,
                    result.input_tokens,
                    result.output_tokens,
                )
            await self._emit_agent(actor, "completed")
            return result
        except asyncio.CancelledError:
            if manual_lifecycle:
                await self._emit_model(execution_id, "failed", actor)
            await self._emit_agent(actor, "failed")
            raise
        except Exception:
            if manual_lifecycle:
                await self._emit_model(execution_id, "failed", actor)
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
    ) -> None:
        callback = self._persist_model_event
        if callback is None:
            return
        args = (execution_id, status, input_tokens, output_tokens, actor)
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            for size in (5, 4, 3, 2):
                candidate = args[:size]
                try:
                    signature.bind(*candidate)
                except TypeError:
                    continue
                await _maybe_await(callback(*candidate))
                return
        await _maybe_await(callback(*args))

    @staticmethod
    def _parse_output(value: Any, output_type: type[BaseModel]) -> BaseModel:
        try:
            return output_type.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise V11RuntimeContractError("invalid V11 model output") from exc

    def _cleanup_session(self, session: AdaptiveToolSession) -> None:
        session._stopped_agents.update(session.task_ids)
        self._active_sessions.discard(session)


def task_id_investigation(repository, task_id: str) -> str:
    """从任务 bucket 找到 investigation owner；用于跨 repository 的最小适配。"""
    for record in repository.list():
        if any(task.id == task_id for task in repository.list_tasks(record.id)):
            return record.id
    raise V11RuntimeContractError("task is not attached to an investigation")


def _session_investigation_id(repository) -> str:
    records = repository.list()
    if len(records) != 1:
        raise V11RuntimeContractError("V11 phase repository must have one investigation")
    return records[0].id


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
