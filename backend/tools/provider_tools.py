from datetime import UTC, datetime
from functools import partial
from time import perf_counter

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceProvider, JsonValue
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.tools.registry import ToolRegistry

_PROVIDER_TOOLS: dict[str, EvidenceProvider] = {
    "read_logs": EvidenceProvider.LOG,
    "query_metrics": EvidenceProvider.METRIC,
    "read_deployments": EvidenceProvider.DEPLOY,
    "read_service_catalog": EvidenceProvider.SERVICE_CATALOG,
    "query_prometheus": EvidenceProvider.METRIC,
    "query_dependencies": EvidenceProvider.DEPENDENCY,
}

_DESCRIPTIONS: dict[str, str] = {
    "read_logs": "Read log evidence.",
    "query_metrics": "Read metric evidence.",
    "read_deployments": "Read deployment evidence.",
    "read_service_catalog": "Read service catalog evidence.",
    "query_prometheus": "Query Prometheus metric evidence.",
    "query_dependencies": "Read dependency evidence.",
    "lookup_memory": "Lookup prior investigation memory.",
}


def build_provider_tool_registry(provider_registry: ProviderRegistry) -> ToolRegistry:
    registry = ToolRegistry()
    provider_handler = partial(invoke_provider_tool, provider_registry)
    for name, provider in _PROVIDER_TOOLS.items():
        registry.register(
            ToolSpec(
                name=name,
                description=_DESCRIPTIONS[name],
                read_only=True,
                provider=provider,
            ),
            provider_handler,
        )
    registry.register(
        ToolSpec(name="lookup_memory", description=_DESCRIPTIONS["lookup_memory"], read_only=True),
        invoke_lookup_memory,
    )
    return registry


def invoke_provider_tool(
    provider_registry: ProviderRegistry,
    *,
    tool_name: str,
    event: IncidentEvent,
    task_id: str,
    agent_name: str,
    input: dict[str, JsonValue] | None = None,
) -> ToolCallRecord:
    provider = _PROVIDER_TOOLS[tool_name]
    started_at = datetime.now(UTC)
    started = perf_counter()
    try:
        results = [
            result
            for result in provider_registry.collect_results(event)
            if result.provider == provider
        ]
        failed = any(result.status == ProviderStatus.FAILED for result in results)
        return _record(
            tool_name=tool_name,
            task_id=task_id,
            agent_name=agent_name,
            input=input,
            started_at=started_at,
            started=started,
            status=ToolCallStatus.FAILED if failed else ToolCallStatus.SUCCESS,
            evidence_ids=[
                item.id for result in results for item in result.evidence_items
            ],
            error_message=_result_message(results),
        )
    except Exception as exc:
        return _record(
            tool_name=tool_name,
            task_id=task_id,
            agent_name=agent_name,
            input=input,
            started_at=started_at,
            started=started,
            status=ToolCallStatus.FAILED,
            evidence_ids=[],
            error_message=str(exc),
        )


def invoke_lookup_memory(
    *,
    tool_name: str,
    event: IncidentEvent,
    task_id: str,
    agent_name: str,
    input: dict[str, JsonValue] | None = None,
) -> ToolCallRecord:
    started_at = datetime.now(UTC)
    started = perf_counter()
    return _record(
        tool_name=tool_name,
        task_id=task_id,
        agent_name=agent_name,
        input=input,
        started_at=started_at,
        started=started,
        status=ToolCallStatus.SUCCESS,
        evidence_ids=[],
        error_message=None,
    )


def _record(
    *,
    tool_name: str,
    task_id: str,
    agent_name: str,
    input: dict[str, JsonValue] | None,
    started_at: datetime,
    started: float,
    status: ToolCallStatus,
    evidence_ids: list[str],
    error_message: str | None,
) -> ToolCallRecord:
    return ToolCallRecord(
        task_id=task_id,
        agent_name=agent_name,
        tool_name=tool_name,
        input=input or {},
        status=status,
        output_evidence_ids=evidence_ids,
        error_message=error_message,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_ms=int((perf_counter() - started) * 1000),
    )


def _result_message(results: list[ProviderResult]) -> str | None:
    messages = [
        result.error_message or f"{result.provider} provider {result.status}"
        for result in results
        if result.status in {ProviderStatus.FAILED, ProviderStatus.PARTIAL}
    ]
    return "; ".join(messages) or None
