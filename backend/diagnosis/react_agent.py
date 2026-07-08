from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.domain.evidence import JsonValue
from backend.domain.react_trace import (
    READ_ONLY_REACT_TOOLS,
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.tools.registry import ToolRegistry


@dataclass(frozen=True)
class LlmToolCall:
    name: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ReActLlmResponse:
    content: str = ""
    tool_call: LlmToolCall | None = None


class ReActLlm(Protocol):
    def generate(
        self, messages: list[dict[str, JsonValue]], tools: list[dict[str, JsonValue]]
    ) -> ReActLlmResponse:
        ...


class ReActInvestigationAgent:
    def __init__(
        self, *, llm: ReActLlm, tool_registry: ToolRegistry, max_steps: int = 5
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps

    def run(
        self,
        *,
        investigation_id: str,
        event: IncidentEvent,
        existing_evidence_ids: list[str],
    ) -> ReActTrace:
        trace = ReActTrace(investigation_id=investigation_id)
        messages = [
            _system_message(event),
            _user_message(event, existing_evidence_ids),
        ]
        specs = self._read_only_specs()
        tools = [_tool_schema(spec) for spec in specs]
        allowed_tool_names = {spec.name for spec in specs}

        for step_number in range(1, self.max_steps + 1):
            response = self.llm.generate(messages, tools)
            if response.tool_call is None:
                trace.status = ReActTraceStatus.COMPLETED
                trace.final_answer = response.content
                trace.completed_at = datetime.now(UTC)
                return trace

            step = self._run_tool_step(
                investigation_id=investigation_id,
                event=event,
                step_number=step_number,
                response=response,
                allowed_tool_names=allowed_tool_names,
            )
            trace.steps.append(step)
            if step.status == ReActTraceStepStatus.FAILED:
                trace.status = ReActTraceStatus.FAILED
                trace.completed_at = datetime.now(UTC)
                return trace

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_call": response.tool_call.name,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "content": step.observation or "",
                    "tool_call_id": step.tool_call_id,
                }
            )

        trace.status = ReActTraceStatus.MAX_STEPS
        trace.completed_at = datetime.now(UTC)
        return trace

    def _read_only_specs(self) -> list[ToolSpec]:
        return [
            spec
            for spec in self.tool_registry.list_specs()
            if spec.name in READ_ONLY_REACT_TOOLS and spec.read_only
        ]

    def _run_tool_step(
        self,
        *,
        investigation_id: str,
        event: IncidentEvent,
        step_number: int,
        response: ReActLlmResponse,
        allowed_tool_names: set[str],
    ) -> ReActTraceStep:
        call = response.tool_call
        if call is None or call.name not in allowed_tool_names:
            return ReActTraceStep(
                step_number=step_number,
                assistant_text=response.content,
                status=ReActTraceStepStatus.FAILED,
                error_message=(
                    f"unsupported ReAct tool: {None if call is None else call.name}"
                ),
                completed_at=datetime.now(UTC),
            )

        tool_input: dict[str, JsonValue] = {"investigation_id": investigation_id}
        try:
            record = self.tool_registry.invoke(
                call.name,
                event=event,
                task_id=f"react-{investigation_id}-{step_number}",
                agent_name="ReActInvestigationAgent",
                input=tool_input,
            )
        except Exception as exc:
            return ReActTraceStep(
                step_number=step_number,
                assistant_text=response.content,
                tool_name=call.name,
                tool_input=tool_input,
                status=ReActTraceStepStatus.FAILED,
                error_message=str(exc),
                completed_at=datetime.now(UTC),
            )

        success = record.status == ToolCallStatus.SUCCESS
        return ReActTraceStep(
            step_number=step_number,
            assistant_text=response.content,
            tool_name=call.name,
            tool_input=tool_input,
            tool_call_id=record.id,
            observation=_observation(record),
            output_evidence_ids=record.output_evidence_ids,
            status=(
                ReActTraceStepStatus.OBSERVED
                if success
                else ReActTraceStepStatus.FAILED
            ),
            error_message=record.error_message,
            completed_at=record.completed_at or datetime.now(UTC),
        )


def _system_message(event: IncidentEvent) -> dict[str, JsonValue]:
    return {
        "role": "system",
        "content": (
            "You are a read-only SRE investigation agent. "
            "Safety boundary: collect and inspect evidence only; do not remediate, "
            "restart, scale, roll back, mutate configuration, use SSH, or execute "
            "commands. Final claims must cite evidence IDs and state uncertainty. "
            f"Service={event.service}; environment={event.environment}; "
            f"severity={event.severity}; started_at={event.started_at}; "
            f"time_window_minutes={event.time_window_minutes}."
        ),
    }


def _user_message(
    event: IncidentEvent, evidence_ids: list[str]
) -> dict[str, JsonValue]:
    return {
        "role": "user",
        "content": (
            f"Title: {event.title}\n"
            f"Description: {event.description}\n"
            f"Existing evidence IDs: {', '.join(evidence_ids) or 'none'}"
        ),
    }


def _tool_schema(spec: ToolSpec) -> dict[str, JsonValue]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": {
            "type": "object",
            "properties": {"investigation_id": {"type": "string"}},
            "required": ["investigation_id"],
        },
        "read_only": True,
    }


def _observation(record: ToolCallRecord) -> str:
    return (
        f"status={record.status}; "
        f"output_evidence_ids={','.join(record.output_evidence_ids) or 'none'}; "
        f"error={record.error_message or 'none'}"
    )
