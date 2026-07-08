from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.domain.evidence import EvidenceProvider, JsonValue


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    read_only: bool = True
    provider: EvidenceProvider | None = None


class ToolCallRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"tool-{uuid4().hex}")
    task_id: str
    agent_name: str
    tool_name: str
    input: dict[str, JsonValue] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.PENDING
    output_evidence_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0)
