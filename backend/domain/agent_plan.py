from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DiagnosisPlan(BaseModel):
    id: str = Field(default_factory=lambda: f"plan-{uuid4().hex}")
    investigation_id: str
    tasks: list[DiagnosisTask] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentExecution(BaseModel):
    id: str = Field(default_factory=lambda: f"exec-{uuid4().hex}")
    task_id: str
    agent_name: str
    status: AgentExecutionStatus = AgentExecutionStatus.PENDING
    tool_call_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    summary: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0)
