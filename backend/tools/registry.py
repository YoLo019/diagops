from collections.abc import Callable
from dataclasses import dataclass

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, JsonValue
from backend.domain.runtime import v11_tool_manifest_hash
from backend.domain.tool_calls import ToolCallRecord, ToolExposure, ToolSpec
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

    def list_agent_specs(self) -> list[ToolSpec]:
        """V11 Agent manifest 的唯一事实来源：只含 Agent 可见的只读工具。"""
        return [
            spec
            for spec in self._specs.values()
            if spec.exposure == ToolExposure.AGENT and spec.read_only
        ]

    def agent_manifest(self) -> tuple[str, ...]:
        """按字典序冻结的 Agent 工具名单，用于契约哈希与调用复核。"""
        return tuple(sorted(spec.name for spec in self.list_agent_specs()))

    def assert_agent_callable(self, tool_name: str, manifest: tuple[str, ...]) -> ToolSpec:
        """V11 调用时复核：目标必须在冻结 manifest 内且 spec 仍为只读。"""
        spec = self.get(tool_name)
        if tool_name not in manifest or spec.exposure != ToolExposure.AGENT:
            raise ValueError(f"tool not in frozen agent manifest: {tool_name}")
        if not spec.read_only:
            raise ValueError(f"agent tool must be read-only: {tool_name}")
        return spec

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


def agent_manifest_hash(manifest: tuple[str, ...]) -> str:
    """为有序 Agent 工具名单生成无凭据的稳定身份。"""
    return v11_tool_manifest_hash(manifest)
