from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.multi_agent import (
    AgentExecutionLayer,
    ExecutionStepKind,
    FailureCategory,
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


class DiagnosisTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class DiagnosisTask(BaseModel):
    id: str = Field(default_factory=lambda: f"task-{uuid4().hex}")
    title: str
    description: str
    task_type: DiagnosisTaskType
    agent_name: str
    tool_names: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0)
    status: DiagnosisTaskStatus = DiagnosisTaskStatus.PENDING
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DiagnosisPlan(BaseModel):
    id: str = Field(default_factory=lambda: f"plan-{uuid4().hex}")
    investigation_id: str
    tasks: list[DiagnosisTask] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentExecution(BaseModel):
    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    id: str = Field(default_factory=lambda: f"exec-{uuid4().hex}")
    task_id: str
    agent_name: str
    status: AgentExecutionStatus = AgentExecutionStatus.PENDING
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] | None = None
    step_kind: ExecutionStepKind | None = None
    attempt: int = Field(default=1, ge=1)
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
    duration_ms: int = Field(default=0, ge=0)
