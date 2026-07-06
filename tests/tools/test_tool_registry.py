from datetime import UTC, datetime

import pytest

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_calls import ToolCallStatus
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult
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
    assert record.error_message == "provider exploded"
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


class FailingProvider:
    provider = EvidenceProvider.LOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        raise RuntimeError("provider exploded")


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
