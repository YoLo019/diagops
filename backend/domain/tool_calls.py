from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer

from backend.domain.evidence import EvidenceProvider, JsonValue


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class ToolExposure(StrEnum):
    AGENT = "agent"
    INTERNAL = "internal"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    read_only: bool = True
    provider: EvidenceProvider | None = None
    # 兼容默认 agent：历史 spec 不声明即视为 Agent 可见；internal 必须显式标记。
    exposure: ToolExposure = ToolExposure.AGENT


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
    runtime_run_id: str | None = None
    logical_call_id: str | None = None
    idempotency_key: str | None = None
    execution_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    # None 沿用历史扣账口径；新调用在准入时记录，拒绝不占取证额度。
    budget_charged: bool | None = None

    @property
    def consumes_budget(self) -> bool:
        """返回逻辑工具调用是否占用额度，兼容没有准入标记的历史记录。"""
        return (
            self.budget_charged
            if self.budget_charged is not None else self.status != ToolCallStatus.PENDING
        )

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.budget_charged is None:
            data.pop("budget_charged", None)
        if self.runtime_run_id is None:
            for field_name in (
                "runtime_run_id",
                "logical_call_id",
                "idempotency_key",
                "execution_id",
                "attempt",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data
