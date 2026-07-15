from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from agents import FunctionTool

from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, JsonValue
from backend.domain.multi_agent import AdaptiveStopReason
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.domain.tool_queries import DependencyQuery, QueryWindow
from backend.providers.results import ProviderResult, ProviderStatus
from backend.safety.redaction import redact_value
from backend.tools.provider_tools import QUERY_MODELS_BY_TOOL
from backend.tools.registry import ToolRegistry

TOOLS_BY_AGENT = {
    AgentName.LOG: frozenset({"read_logs"}),
    AgentName.METRIC: frozenset({"query_metrics", "query_prometheus"}),
    AgentName.DEPLOYMENT: frozenset(
        {"read_deployments", "read_service_catalog", "query_dependencies"}
    ),
}


class AdaptiveToolSession:
    """维护单次 Investigation 的只读工具权限、预算和新增证据。"""

    def __init__(
        self,
        *,
        event: IncidentEvent,
        seed_evidence: list[EvidenceItem],
        registry: ToolRegistry,
        task_ids: dict[AgentName | tuple[AgentName, int], str],
        max_tool_calls_per_specialist: int = 3,
        max_total_tool_calls: int = 8,
        tool_timeout_seconds: int = 10,
        allowed_targets: set[str] | None = None,
    ) -> None:
        self.event = event
        self.registry = registry
        self.task_ids = task_ids
        self.max_tool_calls_per_specialist = max_tool_calls_per_specialist
        self.max_total_tool_calls = max_total_tool_calls
        self.tool_timeout_seconds = tool_timeout_seconds
        self.tool_calls: list[ToolCallRecord] = []
        self.provider_results: list[ProviderResult] = []
        self.new_evidence: list[EvidenceItem] = []
        self.stop_reasons: dict[AgentName, AdaptiveStopReason] = {}
        self._attempts = {name: 0 for name in AgentName}
        self._total_attempts = 0
        self._fingerprints: set[str] = set()
        self._known_evidence_ids = {item.id for item in seed_evidence}
        self._stopped_agents: set[AgentName] = set()
        self._allowed_targets = {event.service, *(allowed_targets or set())}
        for item in seed_evidence:
            dependencies = item.payload.get("dependencies", [])
            if isinstance(dependencies, list):
                self._allowed_targets.update(
                    value for value in dependencies if isinstance(value, str)
                )

    def tools_for(self, agent_name: AgentName, round_number: int) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for tool_name in sorted(TOOLS_BY_AGENT[agent_name]):
            spec = self.registry.get(tool_name)
            if not spec.read_only:
                raise ValueError(f"adaptive tool must be read-only: {tool_name}")

            async def invoke(
                _context,
                raw_input: str,
                *,
                name: str = tool_name,
                agent: AgentName = agent_name,
            ):
                return await self.invoke(agent, name, raw_input, round_number)

            tools.append(
                FunctionTool(
                    name=spec.name,
                    description=spec.description,
                    params_json_schema=dict(spec.input_schema),
                    on_invoke_tool=invoke,
                    strict_json_schema=True,
                    timeout_seconds=self.tool_timeout_seconds,
                    timeout_behavior="error_as_result",
                )
            )
        return tools

    async def invoke(
        self,
        agent_name: AgentName,
        tool_name: str,
        raw_input: str,
        round_number: int,
    ) -> str:
        task_id = self.task_ids.get(
            (agent_name, round_number), self.task_ids[agent_name]
        )
        parsed = self._parse_input(raw_input)
        self._attempts[agent_name] += 1
        self._total_attempts += 1

        if parsed is None:
            return self._reject(
                agent_name,
                tool_name,
                {},
                ToolCallStatus.FAILED,
                "invalid tool input",
                task_id=task_id,
            )
        if tool_name not in TOOLS_BY_AGENT[agent_name]:
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                "tool not allowed for agent",
                task_id=task_id,
            )

        try:
            query = QUERY_MODELS_BY_TOOL[tool_name].model_validate(parsed)
            self._validate_scope(query)
        except (KeyError, TypeError, ValueError):
            return self._reject(
                agent_name,
                tool_name,
                parsed,
                ToolCallStatus.FAILED,
                "tool input outside investigation scope",
                task_id=task_id,
            )

        fingerprint = _query_fingerprint(tool_name, query)
        if fingerprint in self._fingerprints:
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "duplicate query",
                AdaptiveStopReason.DUPLICATE_QUERY,
                task_id=task_id,
            )
        if agent_name in self._stopped_agents:
            reason = self.stop_reasons[agent_name]
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "specialist query stopped",
                reason,
                task_id=task_id,
            )
        if (
            self._attempts[agent_name] > self.max_tool_calls_per_specialist
            or self._total_attempts > self.max_total_tool_calls
        ):
            return self._reject(
                agent_name,
                tool_name,
                query.model_dump(mode="json"),
                ToolCallStatus.SKIPPED,
                "adaptive tool budget exhausted",
                AdaptiveStopReason.BUDGET_EXHAUSTED,
                task_id=task_id,
            )

        self._fingerprints.add(fingerprint)
        result = await asyncio.to_thread(
            self.registry.invoke_detailed,
            tool_name,
            event=self.event,
            task_id=task_id,
            agent_name=agent_name.value,
            input=query.model_dump(mode="json"),
        )
        self.tool_calls.append(result.call)
        self.provider_results.extend(result.provider_results)

        output_ids = set(result.call.output_evidence_ids)
        new_output_ids = output_ids - self._known_evidence_ids
        for item in result.evidence:
            if item.id in self._known_evidence_ids:
                continue
            self._known_evidence_ids.add(item.id)
            self.new_evidence.append(item)

        stop_reason = None
        if not new_output_ids and result.call.status != ToolCallStatus.FAILED:
            stop_reason = AdaptiveStopReason.NO_NEW_EVIDENCE
            self._stop(agent_name, stop_reason)

        return _response(
            status=result.call.status,
            evidence=project_tool_evidence(result.evidence),
            warning=_result_warning(result.provider_results),
            stop_reason=stop_reason,
        )

    def _validate_scope(self, query: QueryWindow) -> None:
        window = timedelta(minutes=self.event.time_window_minutes)
        incident_start = self.event.started_at - window
        incident_end = self.event.started_at + window
        if query.end_time < incident_start or query.start_time > incident_end:
            raise ValueError("query window does not intersect incident window")
        if isinstance(query, DependencyQuery):
            if query.target and query.target not in self._allowed_targets:
                raise ValueError("dependency target outside investigation scope")

    def _reject(
        self,
        agent_name: AgentName,
        tool_name: str,
        input_value: dict[str, Any],
        status: ToolCallStatus,
        message: str,
        stop_reason: AdaptiveStopReason | None = None,
        *,
        task_id: str,
    ) -> str:
        now = datetime.now(UTC)
        safe_input = redact_value(input_value)
        call = ToolCallRecord(
            task_id=task_id,
            agent_name=agent_name.value,
            tool_name=tool_name,
            input=safe_input if isinstance(safe_input, dict) else {},
            status=status,
            error_message=message,
            started_at=now,
            completed_at=now,
        )
        self.tool_calls.append(call)
        if stop_reason is not None:
            self._stop(agent_name, stop_reason)
        return _response(status=status, evidence=[], warning=message, stop_reason=stop_reason)

    def _stop(self, agent_name: AgentName, reason: AdaptiveStopReason) -> None:
        self._stopped_agents.add(agent_name)
        self.stop_reasons[agent_name] = reason

    @staticmethod
    def _parse_input(raw_input: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw_input)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def project_tool_evidence(evidence: list[EvidenceItem]) -> list[dict[str, JsonValue]]:
    """仅向模型暴露定位 Evidence 所需的稳定字段。"""
    return [
        {
            "id": item.id,
            "provider": item.provider.value,
            "kind": item.kind.value,
            "status": item.status.value,
            "timestamp": item.timestamp.isoformat(),
            "summary": item.summary,
        }
        for item in evidence
    ]


def _query_fingerprint(tool_name: str, query: QueryWindow) -> str:
    values = query.model_dump(mode="json", exclude={"reason"})
    canonical = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def _result_warning(results: list[ProviderResult]) -> str | None:
    messages = [
        result.error_message or f"{result.provider.value} provider {result.status.value}"
        for result in results
        if result.status in {ProviderStatus.PARTIAL, ProviderStatus.FAILED, ProviderStatus.SKIPPED}
    ]
    return "; ".join(messages) or None


def _response(
    *,
    status: ToolCallStatus,
    evidence: list[dict[str, JsonValue]],
    warning: str | None,
    stop_reason: AdaptiveStopReason | None,
) -> str:
    return json.dumps(
        {
            "status": status.value,
            "evidence": evidence,
            "warning": warning,
            "stop_reason": stop_reason.value if stop_reason else None,
        },
        ensure_ascii=False,
    )
