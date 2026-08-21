import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from backend.domain.evidence import JsonValue
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    ExecutionActor,
    ExecutionStepKind,
    FailureCategory,
    LeadAction,
    ModelProvider,
    ResultValidationCategory,
)


class DiagnosisTaskType(StrEnum):
    LOG_INVESTIGATION = "log_investigation"
    METRIC_INVESTIGATION = "metric_investigation"
    DEPLOYMENT_CHECK = "deployment_check"
    DEPENDENCY_CHECK = "dependency_check"
    SERVICE_CONTEXT = "service_context"
    MEMORY_LOOKUP = "memory_lookup"
    RCA_SYNTHESIS = "rca_synthesis"
    LLM_REVIEW = "llm_review"
    GENERAL_INVESTIGATION = "general_investigation"


class DiagnosisTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AgentExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


_SELECTED_SKILL_PATTERN = r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,32}$"
SelectedSkill = Annotated[str, Field(pattern=_SELECTED_SKILL_PATTERN)]


class LeadDecision(BaseModel):
    action: LeadAction
    summary: str = Field(min_length=1, max_length=512)
    task_ids: list[str] = Field(default_factory=list, max_length=3)
    candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    selected_skills: list[SelectedSkill] = Field(default_factory=list, max_length=4)
    stop_reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_action_contract(self) -> "LeadDecision":
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("Lead decision task_ids must be unique")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("Lead decision candidate_ids must be unique")
        if any(
            re.fullmatch(_SELECTED_SKILL_PATTERN, item)
            is None
            for item in self.selected_skills
        ):
            raise ValueError("selected_skills must use bounded name@version values")
        if self.action in {LeadAction.INVESTIGATE, LeadAction.TEST} and not self.task_ids:
            raise ValueError("investigate and test require task_ids")
        if self.action == LeadAction.CONCLUDE and not self.candidate_ids:
            raise ValueError("conclude requires candidate_ids")
        if self.action == LeadAction.INCONCLUSIVE:
            if self.task_ids or self.candidate_ids or not self.stop_reason:
                raise ValueError("inconclusive requires no work and a stop_reason")
        return self


class DiagnosisTask(BaseModel):
    id: str = Field(default_factory=lambda: f"task-{uuid4().hex}")
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    task_type: DiagnosisTaskType
    agent_name: str
    tool_names: list[str] = Field(default_factory=list, max_length=9)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0)
    status: DiagnosisTaskStatus = DiagnosisTaskStatus.PENDING
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] | None = None
    strategy: str | None = Field(default=None, max_length=128)
    evidence_scope: dict[str, JsonValue] | None = None
    expected_discriminator: str | None = Field(default=None, max_length=256)
    information_gap: str | None = Field(default=None, max_length=256)
    runtime_run_id: str | None = None
    critic_assessment_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in (
                "strategy",
                "evidence_scope",
                "expected_discriminator",
                "information_gap",
                "runtime_run_id",
                "critic_assessment_id",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data

    @model_validator(mode="after")
    def validate_v11_task(self) -> "DiagnosisTask":
        if self.task_type != DiagnosisTaskType.GENERAL_INVESTIGATION:
            if self.critic_assessment_id is not None and self.analysis_round != 2:
                raise ValueError("critic_assessment_id requires round two")
            return self
        if self.runtime_run_id is None:
            raise ValueError("general investigation task requires runtime_run_id")
        if self.analysis_round is None:
            raise ValueError("general investigation task requires analysis_round")
        if self.analysis_round == 2 and self.critic_assessment_id is None:
            raise ValueError("round two task requires critic_assessment_id")
        if self.analysis_round == 1 and self.critic_assessment_id is not None:
            raise ValueError("round one task cannot use critic_assessment_id")
        if not self.evidence_scope and not self.information_gap:
            raise ValueError(
                "general investigation task requires evidence_scope or information_gap"
            )
        return self


class DiagnosisPlan(BaseModel):
    id: str = Field(default_factory=lambda: f"plan-{uuid4().hex}")
    investigation_id: str
    tasks: list[DiagnosisTask] = Field(default_factory=list)
    runtime_run_id: str | None = None
    lead_decision: LeadDecision | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_v11_plan(self) -> "DiagnosisPlan":
        if self.runtime_run_id is None:
            if self.lead_decision is not None or any(
                task.runtime_run_id is not None for task in self.tasks
            ):
                raise ValueError("V11 plan requires runtime_run_id")
            return self
        if len(self.tasks) > 3:
            raise ValueError("V11 plan allows at most three tasks")
        if any(len(task.tool_names) > 9 for task in self.tasks):
            raise ValueError("V11 task tool_names must contain at most nine tools")
        if any(task.runtime_run_id != self.runtime_run_id for task in self.tasks):
            raise ValueError("V11 plan tasks must share runtime_run_id")
        if self.lead_decision is not None and self.lead_decision.action == LeadAction.CONCLUDE:
            raise ValueError("planning cannot conclude")
        if self.lead_decision is not None and not set(
            self.lead_decision.task_ids
        ) <= {task.id for task in self.tasks}:
            raise ValueError("Lead decision references a task outside the plan")
        if self.lead_decision is not None and self.lead_decision.action == LeadAction.TEST:
            planned_tasks = {task.id: task for task in self.tasks}
            if any(
                not planned_tasks[task_id].expected_discriminator
                or not planned_tasks[task_id].expected_discriminator.strip()
                for task_id in self.lead_decision.task_ids
            ):
                raise ValueError("test tasks require expected_discriminator")
        return self

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in ("runtime_run_id", "lead_decision"):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data


class AgentExecution(BaseModel):
    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    id: str = Field(default_factory=lambda: f"exec-{uuid4().hex}")
    task_id: str
    agent_name: str
    runtime_run_id: str | None = None
    status: AgentExecutionStatus = AgentExecutionStatus.PENDING
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] | None = None
    step_kind: ExecutionStepKind | None = None
    attempt: int = Field(default=1, ge=1)
    runtime_attempt_id: str | None = None
    resume_from_execution_id: str | None = None
    failure_category: FailureCategory = FailureCategory.NONE
    result_validation_category: ResultValidationCategory | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    tool_call_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline_at: datetime | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_v11_execution(self) -> "AgentExecution":
        v11_actor = self.agent_name in {item.value for item in ExecutionActor}
        # V11 writer 会显式写入 owner 字段；历史 RESULT_VALIDATION payload 省略该字段，需保持可读。
        v11_step = self.step_kind in {
            ExecutionStepKind.LEAD_PLANNING,
            ExecutionStepKind.INVESTIGATOR_ANALYSIS,
            ExecutionStepKind.CRITIC_REVIEW,
            ExecutionStepKind.LEAD_ADJUDICATION,
        } or (
            self.step_kind == ExecutionStepKind.RESULT_VALIDATION
            and (
                self.analysis_round is not None
                or "runtime_run_id" in self.model_fields_set
            )
        )
        if (v11_actor or v11_step) and self.runtime_run_id is None:
            raise ValueError("V11 execution requires runtime_run_id")
        return self

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in (
                "runtime_attempt_id",
                "resume_from_execution_id",
                "deadline_at",
                "input_tokens",
                "output_tokens",
                "runtime_run_id",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data
