import math
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from backend.domain.evidence import JsonValue

READ_ONLY_REACT_TOOLS = frozenset(
    {
        "read_logs",
        "query_metrics",
        "read_deployments",
        "query_dependencies",
        "read_service_catalog",
        "lookup_memory",
    }
)


class ReActTraceStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_STEPS = "max_steps"
    DISABLED = "disabled"


class ReActTraceStepStatus(StrEnum):
    THINKING = "thinking"
    TOOL_CALLED = "tool_called"
    OBSERVED = "observed"
    COMPLETED = "completed"
    FAILED = "failed"


class ReActTraceStep(BaseModel):
    step_number: int = Field(ge=1)
    assistant_text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, JsonValue] = Field(default_factory=dict)
    tool_call_id: str | None = None
    observation: str | None = None
    output_evidence_ids: list[str] = Field(default_factory=list)
    status: ReActTraceStepStatus = ReActTraceStepStatus.THINKING
    error_message: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("tool_name")
    @classmethod
    def tool_must_be_read_only(cls, value: str | None) -> str | None:
        if value is not None and value not in READ_ONLY_REACT_TOOLS:
            raise ValueError(f"unsupported ReAct tool: {value}")
        return value

    @field_validator("tool_input")
    @classmethod
    def reject_non_finite_tool_input_floats(
        cls, tool_input: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        def validate_json_value(value: JsonValue) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("tool_input must not contain non-finite float values")
            if isinstance(value, list):
                for item in value:
                    validate_json_value(item)
            if isinstance(value, dict):
                for item in value.values():
                    validate_json_value(item)

        validate_json_value(tool_input)
        return tool_input


class ReActTrace(BaseModel):
    id: str = Field(default_factory=lambda: f"react-{uuid4().hex}")
    investigation_id: str
    status: ReActTraceStatus = ReActTraceStatus.RUNNING
    final_answer: str | None = None
    steps: list[ReActTraceStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("steps")
    @classmethod
    def order_steps(cls, value: list[ReActTraceStep]) -> list[ReActTraceStep]:
        return sorted(value, key=lambda step: step.step_number)
