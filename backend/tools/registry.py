from collections.abc import Callable
from dataclasses import dataclass

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, JsonValue
from backend.domain.tool_calls import ToolCallRecord, ToolSpec
from backend.providers.results import ProviderResult


@dataclass(frozen=True)
class ToolInvocationResult:
    call: ToolCallRecord
    evidence: list[EvidenceItem]
    provider_results: list[ProviderResult]


ToolHandler = Callable[..., ToolInvocationResult | ToolCallRecord]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def invoke(
        self,
        tool_name: str,
        *,
        event: IncidentEvent,
        task_id: str,
        agent_name: str,
        input: dict[str, JsonValue] | None = None,
    ) -> ToolCallRecord:
        return self.invoke_detailed(
            tool_name,
            event=event,
            task_id=task_id,
            agent_name=agent_name,
            input=input,
        ).call

    def invoke_detailed(
        self,
        tool_name: str,
        *,
        event: IncidentEvent,
        task_id: str,
        agent_name: str,
        input: dict[str, JsonValue] | None = None,
    ) -> ToolInvocationResult:
        if tool_name not in self._handlers:
            raise ValueError(f"unknown tool: {tool_name}")
        result = self._handlers[tool_name](
            tool_name=tool_name,
            event=event,
            task_id=task_id,
            agent_name=agent_name,
            input=input,
        )
        if isinstance(result, ToolCallRecord):
            return ToolInvocationResult(call=result, evidence=[], provider_results=[])
        return result
