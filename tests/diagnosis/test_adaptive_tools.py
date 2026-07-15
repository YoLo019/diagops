import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from backend.diagnosis.adaptive_tools import AdaptiveToolSession, project_tool_evidence
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult
from backend.tools.provider_tools import build_provider_tool_registry


@pytest.mark.anyio
async def test_session_enforces_scope_budget_and_duplicate_fingerprint():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    session = _session([provider])
    payload = _query_payload(
        reason="first",
        keywords=["Timeout", "error"],
        levels=["ERROR", "Critical"],
    )

    first = json.loads(
        await session.invoke(AgentName.LOG, "read_logs", json.dumps(payload), 1)
    )
    duplicate = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(
                {
                    **payload,
                    "reason": "same query, reordered parameters",
                    "start_time": "2026-07-15T15:50:00+08:00",
                    "end_time": "2026-07-15T16:10:00+08:00",
                    "keywords": ["ERROR", "timeout"],
                    "levels": ["critical", "error"],
                }
            ),
            1,
        )
    )
    wrong_agent = json.loads(
        await session.invoke(AgentName.METRIC, "read_logs", json.dumps(payload), 1)
    )
    assert first["status"] == "success"
    assert duplicate["status"] == "skipped"
    assert wrong_agent["status"] == "failed"
    assert provider.calls == 1
    assert [call.status for call in session.tool_calls] == [
        ToolCallStatus.SUCCESS,
        ToolCallStatus.SKIPPED,
        ToolCallStatus.FAILED,
    ]


@pytest.mark.anyio
async def test_session_enforces_per_specialist_budget():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    session = _session([provider], per_agent=1)

    await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload(keywords=["first"])), 1
    )
    exhausted = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload(keywords=["second"])),
            1,
        )
    )

    assert exhausted["status"] == "skipped"
    assert exhausted["stop_reason"] == "budget_exhausted"
    assert provider.calls == 1


@pytest.mark.anyio
async def test_session_enforces_global_budget_across_specialists():
    log = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    metric = QueryProvider("query_metrics", EvidenceProvider.METRIC, "ev-metric")
    session = _session([log, metric], total=1)

    await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload(keywords=["first"])), 1
    )
    exhausted = json.loads(
        await session.invoke(
            AgentName.METRIC, "query_metrics", json.dumps(_query_payload()), 1
        )
    )

    assert exhausted["stop_reason"] == "budget_exhausted"
    assert metric.calls == 0


@pytest.mark.anyio
async def test_session_rejects_out_of_window_and_out_of_scope_dependency():
    provider = QueryProvider(
        "query_dependencies", EvidenceProvider.DEPENDENCY, "ev-dependency"
    )
    seed = _evidence(
        "ev-catalog",
        EvidenceProvider.SERVICE_CATALOG,
        EvidenceKind.SERVICE_METADATA,
        payload={"dependencies": ["inventory-service"]},
    )
    session = _session([provider], seed=[seed])

    outside = _query_payload(
        start_time="2026-07-14T00:00:00+00:00",
        end_time="2026-07-14T00:30:00+00:00",
        target="inventory-service",
    )
    forbidden = _query_payload(target="unknown-service")
    allowed = _query_payload(target="inventory-service")

    assert json.loads(
        await session.invoke(
            AgentName.DEPLOYMENT, "query_dependencies", json.dumps(outside), 1
        )
    )["status"] == "failed"
    assert json.loads(
        await session.invoke(
            AgentName.DEPLOYMENT, "query_dependencies", json.dumps(forbidden), 1
        )
    )["status"] == "failed"
    assert json.loads(
        await session.invoke(
            AgentName.DEPLOYMENT, "query_dependencies", json.dumps(allowed), 1
        )
    )["status"] == "success"
    assert provider.calls == 1


@pytest.mark.anyio
async def test_session_allows_targets_from_seed_dependency_edges():
    provider = QueryProvider(
        "query_dependencies", EvidenceProvider.DEPENDENCY, "ev-follow-up"
    )
    seed = _evidence(
        "ev-openrca-edge",
        EvidenceProvider.DEPENDENCY,
        EvidenceKind.DEPENDENCY_HEALTH,
        payload={
            "edges": [
                {"parent": "checkout-service", "child": "payment-service"}
            ]
        },
    )
    session = _session([provider], seed=[seed])

    response = json.loads(
        await session.invoke(
            AgentName.DEPLOYMENT,
            "query_dependencies",
            json.dumps(_query_payload(target="payment-service")),
            1,
        )
    )

    assert response["status"] == "success"
    assert provider.calls == 1


@pytest.mark.anyio
async def test_empty_result_stops_later_queries_for_specialist():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, None)
    session = _session([provider])

    first = json.loads(
        await session.invoke(
            AgentName.LOG, "read_logs", json.dumps(_query_payload()), 1
        )
    )
    later = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload(keywords=["next"])),
            1,
        )
    )

    assert first["stop_reason"] == "no_new_evidence"
    assert later["status"] == "skipped"
    assert provider.calls == 1


@pytest.mark.anyio
async def test_failed_tool_can_be_corrected_with_remaining_budget():
    provider = FailingOnceProvider()
    session = _session([provider])

    failed = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload(keywords=["first"])),
            1,
        )
    )
    corrected = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload(keywords=["corrected"])),
            1,
        )
    )

    assert failed["status"] == "failed"
    assert corrected["status"] == "success"
    assert provider.calls == 2


@pytest.mark.anyio
async def test_session_records_provider_timeout_before_sdk_cancels_tool():
    provider = SlowProvider()
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([provider])),
        task_ids=_task_ids(),
        tool_timeout_seconds=0.01,
    )

    response = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload()),
            1,
        )
    )

    assert response["status"] == "failed"
    assert response["stop_reason"] == "timeout"
    assert session.tool_calls[0].status == ToolCallStatus.FAILED
    assert "timeout" in (session.tool_calls[0].error_message or "")
    await asyncio.sleep(0.06)
    assert len(session.tool_calls) == 1
    assert session.provider_results == []
    assert session.new_evidence == []


@pytest.mark.anyio
async def test_tool_call_uses_round_specific_task_id():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-round-two")
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([provider])),
        task_ids={
            AgentName.LOG: "task-log-default",
            (AgentName.LOG, 2): "task-log-round-two",
        },
    )

    await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload()), round_number=2
    )

    assert session.tool_calls[0].task_id == "task-log-round-two"


def test_projection_excludes_payload_and_secrets():
    projected = project_tool_evidence(
        [
            _evidence(
                "ev-secret",
                EvidenceProvider.LOG,
                EvidenceKind.LOG_PATTERN,
                payload={"token": "secret-value"},
            )
        ]
    )

    assert set(projected[0]) == {
        "id",
        "provider",
        "kind",
        "status",
        "timestamp",
        "summary",
    }
    assert "secret-value" not in str(projected)


def test_tools_for_rejects_non_read_only_spec():
    registry = build_provider_tool_registry(ProviderRegistry([]))
    registry.register(
        ToolSpec(name="read_logs", description="unsafe", read_only=False),
        lambda **kwargs: ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
        ),
    )
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=registry,
        task_ids=_task_ids(),
    )

    with pytest.raises(ValueError, match="read-only"):
        session.tools_for(AgentName.LOG, 1)


class QueryProvider:
    def __init__(
        self,
        tool_name: str,
        provider: EvidenceProvider,
        evidence_id: str | None,
    ) -> None:
        self.supported_tools = frozenset({tool_name})
        self.provider = provider
        self.evidence_id = evidence_id
        self.calls = 0

    def collect(self, event, query):
        self.calls += 1
        evidence = []
        if self.evidence_id:
            kind = {
                EvidenceProvider.LOG: EvidenceKind.LOG_PATTERN,
                EvidenceProvider.DEPENDENCY: EvidenceKind.DEPENDENCY_HEALTH,
            }[self.provider]
            evidence = [
                _evidence(self.evidence_id, self.provider, kind, timestamp=query.end_time)
            ]
        return ProviderResult(provider=self.provider, evidence_items=evidence)


class FailingOnceProvider(QueryProvider):
    def __init__(self):
        super().__init__("read_logs", EvidenceProvider.LOG, "ev-corrected")

    def collect(self, event, query):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider failed")
        return ProviderResult(
            provider=self.provider,
            evidence_items=[
                _evidence(
                    self.evidence_id,
                    self.provider,
                    EvidenceKind.LOG_PATTERN,
                    timestamp=query.end_time,
                )
            ],
        )


class SlowProvider(QueryProvider):
    def __init__(self):
        super().__init__("read_logs", EvidenceProvider.LOG, "ev-too-late")

    def collect(self, event, query):
        time.sleep(0.05)
        return super().collect(event, query)


def _session(providers, *, seed=None, per_agent=3, total=8):
    return AdaptiveToolSession(
        event=_event(),
        seed_evidence=seed or [],
        registry=build_provider_tool_registry(ProviderRegistry(providers)),
        task_ids=_task_ids(),
        max_tool_calls_per_specialist=per_agent,
        max_total_tool_calls=total,
    )


def _task_ids():
    return {name: f"task-{name.value}" for name in AgentName}


def _query_payload(**overrides):
    values = {
        "start_time": "2026-07-15T07:50:00+00:00",
        "end_time": "2026-07-15T08:10:00+00:00",
        "reason": "验证候选根因",
    }
    values.update(overrides)
    return values


def _event():
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="Users see 500s.",
        started_at=datetime(2026, 7, 15, 8, tzinfo=UTC),
        time_window_minutes=30,
    )


def _evidence(evidence_id, provider, kind, *, payload=None, timestamp=None):
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kind,
        timestamp=timestamp or datetime(2026, 7, 15, 8, tzinfo=UTC),
        summary="bounded evidence",
        payload=payload or {},
    )
