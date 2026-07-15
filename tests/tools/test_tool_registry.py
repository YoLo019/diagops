from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_calls import ToolCallStatus
from backend.domain.tool_queries import LogQuery
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.tools.provider_tools import build_provider_tool_registry


def test_provider_tool_specs_exist_and_are_read_only():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    specs = {spec.name: spec for spec in registry.list_specs()}

    assert set(specs) >= {
        "read_logs",
        "query_metrics",
        "read_deployments",
        "read_service_catalog",
        "query_prometheus",
        "lookup_memory",
        "query_dependencies",
    }
    assert all(spec.read_only for spec in specs.values())


def test_tool_specs_publish_strict_pydantic_schema():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    schema = registry.get("read_logs").input_schema

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"start_time", "end_time", "reason"}


def test_parameterized_tool_uses_only_declared_capability():
    log = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    metric = QueryProvider("query_metrics", EvidenceProvider.METRIC, "ev-metric")
    registry = build_provider_tool_registry(ProviderRegistry([log, metric]))
    incident = event()

    result = registry.invoke_detailed(
        "read_logs",
        event=incident,
        task_id="task-log",
        agent_name="LogAgent",
        input={
            "start_time": incident.started_at.isoformat(),
            "end_time": (incident.started_at + timedelta(minutes=15)).isoformat(),
            "reason": "验证超时日志",
            "keywords": ["timeout"],
        },
    )

    assert result.call.output_evidence_ids == ["ev-log"]
    assert [item.id for item in result.evidence] == ["ev-log"]
    assert log.calls == 1
    assert metric.calls == 0
    assert isinstance(log.query, LogQuery)


@pytest.mark.parametrize(
    ("tool_name", "expected_id"),
    [
        ("read_logs", "ev-log"),
        ("query_metrics", "ev-metric"),
        ("read_deployments", "ev-deploy"),
        ("read_service_catalog", "ev-service-catalog"),
        ("query_dependencies", "ev-dependency"),
        ("query_prometheus", "ev-metric"),
    ],
)
def test_provider_tools_return_only_matching_provider_evidence(tool_name, expected_id):
    registry = build_provider_tool_registry(
        ProviderRegistry(
            [
                StaticProvider(EvidenceProvider.LOG, "ev-log"),
                StaticProvider(EvidenceProvider.METRIC, "ev-metric"),
                StaticProvider(EvidenceProvider.DEPLOY, "ev-deploy"),
                StaticProvider(EvidenceProvider.SERVICE_CATALOG, "ev-service-catalog"),
                StaticProvider(EvidenceProvider.DEPENDENCY, "ev-dependency"),
            ]
        )
    )

    record = registry.invoke(
        tool_name,
        event=event(),
        task_id="task-1",
        agent_name="LogAgent",
    )

    assert record.status == ToolCallStatus.SUCCESS
    assert record.output_evidence_ids == [expected_id]
    assert record.duration_ms >= 0


def test_provider_exception_returns_failed_tool_call_with_error_message():
    registry = build_provider_tool_registry(ProviderRegistry([FailingProvider()]))

    record = registry.invoke(
        "read_logs",
        event=event(),
        task_id="task-1",
        agent_name="LogAgent",
    )

    assert record.status == ToolCallStatus.FAILED
    assert record.error_message == "provider collection failed"
    assert "exploded" not in record.error_message
    assert record.output_evidence_ids == []


def test_provider_tool_does_not_invoke_non_matching_provider():
    non_target = CountingFailingProvider(EvidenceProvider.METRIC)
    registry = build_provider_tool_registry(
        ProviderRegistry(
            [
                StaticProvider(EvidenceProvider.LOG, "ev-log"),
                non_target,
            ]
        )
    )

    record = registry.invoke(
        "read_logs",
        event=event(),
        task_id="task-1",
        agent_name="LogAgent",
    )

    assert record.status == ToolCallStatus.SUCCESS
    assert record.output_evidence_ids == ["ev-log"]
    assert non_target.calls == 0


def test_failed_provider_result_returns_failed_tool_call_with_error_message():
    registry = build_provider_tool_registry(ProviderRegistry([FailedResultProvider()]))

    record = registry.invoke(
        "read_logs",
        event=event(),
        task_id="task-1",
        agent_name="LogAgent",
    )

    assert record.status == ToolCallStatus.FAILED
    assert record.error_message == "log degraded"
    assert record.output_evidence_ids == []


def test_unknown_tool_raises_value_error():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    with pytest.raises(ValueError, match="unknown tool"):
        registry.invoke(
            "missing_tool",
            event=event(),
            task_id="task-1",
            agent_name="LogAgent",
        )


def test_lookup_memory_is_empty_success_for_now():
    registry = build_provider_tool_registry(ProviderRegistry([]))

    record = registry.invoke(
        "lookup_memory",
        event=event(),
        task_id="task-1",
        agent_name="MemoryAgent",
    )

    assert record.status == ToolCallStatus.SUCCESS
    assert record.output_evidence_ids == []
    assert record.error_message is None
    assert record.duration_ms >= 0


class StaticProvider:
    def __init__(self, provider: EvidenceProvider, evidence_id: str) -> None:
        self.provider = provider
        self.evidence_id = evidence_id

    def collect(self, event: IncidentEvent) -> ProviderResult:
        return ProviderResult(
            provider=self.provider,
            evidence_items=[
                EvidenceItem(
                    id=self.evidence_id,
                    provider=self.provider,
                    kind=_KIND_BY_PROVIDER[self.provider],
                    timestamp=event.started_at,
                    summary=f"{self.provider} evidence",
                )
            ],
        )


class QueryProvider:
    def __init__(
        self, tool_name: str, provider: EvidenceProvider, evidence_id: str
    ) -> None:
        self.supported_tools = frozenset({tool_name})
        self.provider = provider
        self.evidence_id = evidence_id
        self.calls = 0
        self.query = None

    def collect(self, event: IncidentEvent, query) -> ProviderResult:
        self.calls += 1
        self.query = query
        return ProviderResult(
            provider=self.provider,
            evidence_items=[
                EvidenceItem(
                    id=self.evidence_id,
                    provider=self.provider,
                    kind=_KIND_BY_PROVIDER[self.provider],
                    timestamp=event.started_at,
                    summary=f"{self.provider} query evidence",
                )
            ],
        )


class FailingProvider:
    provider = EvidenceProvider.LOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        raise RuntimeError("provider exploded")


class CountingFailingProvider:
    def __init__(self, provider: EvidenceProvider) -> None:
        self.provider = provider
        self.calls = 0

    def collect(self, event: IncidentEvent) -> ProviderResult:
        self.calls += 1
        raise AssertionError("non-target provider should not be called")


class FailedResultProvider:
    provider = EvidenceProvider.LOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        return ProviderResult(
            provider=self.provider,
            status=ProviderStatus.FAILED,
            error_message="log degraded",
        )


_KIND_BY_PROVIDER = {
    EvidenceProvider.LOG: EvidenceKind.LOG_PATTERN,
    EvidenceProvider.METRIC: EvidenceKind.METRIC_TREND,
    EvidenceProvider.DEPLOY: EvidenceKind.DEPLOYMENT,
    EvidenceProvider.SERVICE_CATALOG: EvidenceKind.SERVICE_METADATA,
    EvidenceProvider.DEPENDENCY: EvidenceKind.DEPENDENCY_HEALTH,
}


def event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="Users see 500s.",
        started_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
    )
