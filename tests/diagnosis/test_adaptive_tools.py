import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
import openai
import pytest

from backend.diagnosis.adaptive_tools import AdaptiveToolSession, project_tool_evidence
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
)
from backend.domain.multi_agent import FailureCategory
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
async def test_scoped_telemetry_and_memory_tools_dispatch_without_window():
    session = _manifest_session(
        [
            QueryProvider("query_related_alerts", EvidenceProvider.RELATED_ALERT, None),
            QueryProvider("query_traces", EvidenceProvider.TRACE, None),
            QueryProvider("read_runtime_state", EvidenceProvider.RUNTIME_STATE, None),
            QueryProvider("lookup_memory", EvidenceProvider.VERIFIED_INCIDENT, None),
        ]
    )

    for index, tool_name in enumerate(
        (
            "query_related_alerts",
            "query_traces",
            "read_runtime_state",
            "lookup_memory",
        ),
        start=1,
    ):
        response = json.loads(
            await session.invoke(
                f"investigator-{index}", tool_name, json.dumps({}), 1
            )
        )
        assert response["status"] == "success", tool_name


@pytest.mark.anyio
async def test_scoped_window_intersection_is_enforced_without_attribute_error():
    provider = QueryProvider(
        "query_related_alerts", EvidenceProvider.RELATED_ALERT, None
    )
    session = _manifest_session([provider])

    outside = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps(
                {
                    "window_start": "2026-07-15T06:00:00+00:00",
                    "window_end": "2026-07-15T06:30:00+00:00",
                }
            ),
            1,
        )
    )
    assert outside["status"] == "failed"
    assert outside["warning"] == "tool input outside investigation scope"
    assert provider.calls == 0


@pytest.mark.anyio
async def test_invalid_scoped_window_reports_the_schema_fields_to_the_model():
    provider = QueryProvider("read_runtime_state", EvidenceProvider.RUNTIME_STATE, None)
    session = _manifest_session([provider])

    response = json.loads(
        await session.invoke(
            "investigator-1",
            "read_runtime_state",
            json.dumps(
                {
                    "start_time": "2026-07-15T07:50:00+00:00",
                    "end_time": "2026-07-15T08:10:00+00:00",
                }
            ),
            1,
        )
    )

    assert response["status"] == "failed"
    assert response["warning"].startswith("invalid tool input:")
    assert "window_start" in response["warning"]
    assert "window_end" in response["warning"]
    assert provider.calls == 0


@pytest.mark.anyio
async def test_scoped_window_fingerprint_normalizes_timezone_spelling():
    provider = QueryProvider(
        "query_related_alerts", EvidenceProvider.RELATED_ALERT, None
    )
    session = _manifest_session([provider])
    base = {
        "window_start": "2026-07-15T07:50:00+00:00",
        "window_end": "2026-07-15T08:10:00+00:00",
    }

    first = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps(base),
            1,
        )
    )
    duplicate = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps(
                {
                    "window_start": "2026-07-15T15:50:00+08:00",
                    "window_end": "2026-07-15T16:10:00+08:00",
                }
            ),
            1,
        )
    )
    assert first["status"] == "success"
    assert duplicate["status"] == "skipped"
    assert provider.calls == 1


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
async def test_distinct_queries_have_distinct_stable_logical_operations():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    session = _session([provider], per_agent=2, total=2)

    await session.invoke(
        AgentName.LOG,
        "read_logs",
        json.dumps(_query_payload(keywords=["first"])),
        1,
    )
    await session.invoke(
        AgentName.LOG,
        "read_logs",
        json.dumps(_query_payload(keywords=["second"])),
        1,
    )

    logical_ids = [call.logical_call_id for call in session.tool_calls]
    assert provider.calls == 2
    assert len(set(logical_ids)) == 2


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
async def test_session_allows_service_target_from_scoped_pod_evidence():
    provider = QueryProvider(
        "query_dependencies", EvidenceProvider.DEPENDENCY, "ev-follow-up-pod"
    )
    seed = _evidence(
        "ev-runtime-pod",
        EvidenceProvider.RUNTIME_STATE,
        EvidenceKind.RUNTIME_STATE,
        payload={
            "entity_id": "queue-master-5f47847cd-4wbwd",
            "runtime_kind": "pod",
        },
    )
    session = _session([provider], seed=[seed])

    response = json.loads(
        await session.invoke(
            AgentName.DEPLOYMENT,
            "query_dependencies",
            json.dumps(_query_payload(target="queue-master")),
            1,
        )
    )

    assert response["status"] == "success"
    assert provider.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("evidence_id", [None, "ev-known"])
async def test_empty_or_known_result_does_not_stop_later_queries_for_specialist(evidence_id):
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, evidence_id)
    seed = [_evidence("ev-known", EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN)]
    session = _session([provider], seed=seed, per_agent=4)

    first = json.loads(
        await session.invoke(
            AgentName.LOG, "read_logs", json.dumps(_query_payload()), 1
        )
    )
    duplicate = json.loads(await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload()), 1,
    ))
    later = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload(keywords=["next"])),
            1,
        )
    )

    assert first["stop_reason"] is None
    assert "no new evidence" in first["warning"]
    if evidence_id is None:
        assert "coverage is unverified, not evidence of health" in first["warning"]
        assert "keywords use AND" in first["warning"]
    else:
        assert "previously seen evidence" in first["warning"]
        assert [item.id for item in session.retrieved_evidence()] == ["ev-known"]
        assert session.new_evidence == []
    assert duplicate["status"] == "skipped"
    assert duplicate["stop_reason"] is None
    assert later["status"] == "success"
    assert provider.calls == 2


@pytest.mark.anyio
async def test_empty_filtered_trace_query_preserves_filters_and_allows_coverage_query():
    class TraceProvider:
        provider = EvidenceProvider.TRACE
        supported_tools = frozenset({"query_traces"})

        def __init__(self):
            self.queries = []

        def collect(self, event, query):
            self.queries.append(query)
            evidence = [] if query.error_only or query.min_duration_ms else [
                _evidence("fast-server", self.provider, EvidenceKind.TRACE_LATENCY,
                          payload={"service": event.service, "duration_ms": 0.1,
                                   "status": "ok"}),
            ]
            return ProviderResult(provider=self.provider, evidence_items=evidence)

    provider = TraceProvider()
    session = _manifest_session([provider])
    filtered = json.loads(await session.invoke(
        "investigator-1", "query_traces",
        json.dumps({"service": "checkout-service", "error_only": True, "min_duration_ms": 400}),
        1,
    ))
    assert filtered["evidence"] == []
    assert "fast successful server spans" in filtered["warning"]
    assert filtered["stop_reason"] is None
    assert provider.queries[0].min_duration_ms == 400
    assert provider.queries[0].error_only
    coverage = json.loads(await session.invoke(
        "investigator-1", "query_traces", '{"service":"checkout-service"}', 1,
    ))
    assert coverage["evidence"][0]["id"] == "fast-server"
    assert len(provider.queries) == 2


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
async def test_session_retries_only_transport_once_and_persists_attempts():
    provider = RateLimitedOnceProvider()
    session = _session([provider], total=1)

    response = json.loads(
        await session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload()),
            1,
        )
    )

    assert response["status"] == "success"
    assert provider.calls == 2
    assert [call.attempt for call in session.tool_calls] == [1, 2]
    assert [call.status for call in session.tool_calls] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCESS,
    ]
    assert session.tool_calls[0].logical_call_id == session.tool_calls[1].logical_call_id


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
async def test_session_deadline_preflight_blocks_tool_before_provider_start():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-deadline")
    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([provider])),
        task_ids=_task_ids(),
        remaining_deadline_seconds=lambda: 0.0,
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
    assert provider.calls == 0
    assert session.tool_calls == []


@pytest.mark.anyio
async def test_session_persists_interrupted_tool_before_propagating_cancel():
    persisted_calls = []
    started = asyncio.Event()

    async def persist_start(call):
        persisted_calls.append(call)
        started.set()
        return call

    async def persist_result(result):
        persisted_calls.append(result.call)
        return result.call

    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([SlowProvider()])),
        task_ids=_task_ids(),
        persist_tool_start=persist_start,
        persist_tool_result=persist_result,
    )
    invocation = asyncio.create_task(
        session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload()),
            1,
        )
    )
    await started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert [call.status for call in persisted_calls] == [
        ToolCallStatus.RUNNING,
        ToolCallStatus.INTERRUPTED,
    ]
    assert persisted_calls[0].id == persisted_calls[1].id


@pytest.mark.anyio
async def test_session_finishes_timeout_persistence_when_sdk_cancels_cleanup():
    persisted_calls = []
    persistence_started = asyncio.Event()
    persistence_release = asyncio.Event()

    async def persist_start(call):
        persisted_calls.append(call)
        return call

    async def persist_result(result):
        persistence_started.set()
        await persistence_release.wait()
        persisted_calls.append(result.call)
        return result.call

    session = AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([SlowProvider()])),
        task_ids=_task_ids(),
        tool_timeout_seconds=0.01,
        persist_tool_start=persist_start,
        persist_tool_result=persist_result,
    )
    invocation = asyncio.create_task(
        session.invoke(
            AgentName.LOG,
            "read_logs",
            json.dumps(_query_payload()),
            1,
        )
    )
    await persistence_started.wait()
    invocation.cancel()
    persistence_release.set()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert [call.status for call in persisted_calls] == [
        ToolCallStatus.RUNNING,
        ToolCallStatus.FAILED,
    ]
    assert persisted_calls[0].id == persisted_calls[1].id


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
        "scope_entity_ids",
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


def test_tools_for_exposes_compact_schema_but_keeps_query_contract():
    from backend.diagnosis.adaptive_tools import _compact_json_schema

    registry = build_provider_tool_registry(ProviderRegistry([]))
    original = registry.get("query_traces").input_schema
    compact = _compact_json_schema(original)

    assert len(json.dumps(compact, sort_keys=True)) < len(
        json.dumps(original, sort_keys=True)
    )
    assert compact["additionalProperties"] is False
    assert compact.get("required", []) == original.get("required", [])
    assert set(compact["properties"]) == set(original["properties"])
    assert "$defs" not in json.dumps(compact)


@pytest.mark.anyio
async def test_model_query_schema_uses_server_owned_incident_window_defaults():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-default-window")
    session = _manifest_session([provider])

    tool = next(
        item for item in session.tools_for("investigator-1", 1)
        if item.name == "read_logs"
    )
    assert not set(tool.params_json_schema.get("required", [])) & {
        "start_time",
        "end_time",
        "reason",
        "limit",
    }
    assert not set(tool.params_json_schema.get("properties", [])) & {
        "start_time",
        "end_time",
        "reason",
        "limit",
    }

    response = json.loads(
        await session.invoke("investigator-1", "read_logs", "{}", 1)
    )
    assert response["status"] == "success"
    assert provider.calls == 1


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


class RateLimitedOnceProvider(QueryProvider):
    def __init__(self):
        super().__init__("read_logs", EvidenceProvider.LOG, "ev-retried")

    def collect(self, event, query):
        self.calls += 1
        if self.calls == 1:
            request = httpx.Request("GET", "https://provider.test/logs")
            response = httpx.Response(429, request=request)
            raise openai.RateLimitError("limited", response=response, body={})
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


def _manifest_session(providers):
    from types import SimpleNamespace

    registry = build_provider_tool_registry(
        ProviderRegistry(providers),
        memory_lookup=SimpleNamespace(lookup=lambda _event, _query: []),
    )
    return AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=registry,
        task_ids={
            f"investigator-{index}": f"task-timeline-{index}" for index in range(1, 5)
        },
        agent_manifest=registry.agent_manifest(),
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


def test_model_behavior_error_is_retryable_invalid_output():
    # 网关/模型输出畸形 JSON（ModelBehaviorError）应分类为 invalid_output 并允许
    # 一次重试；不再一次畸形响应直接杀 run。
    from agents.exceptions import ModelBehaviorError

    from backend.diagnosis.adaptive_tools import retryable_failure_category

    assert (
        retryable_failure_category(ModelBehaviorError("bad json"))
        == FailureCategory.INVALID_OUTPUT
    )


def test_openai_api_timeout_is_retryable_model_transport_noise():
    import httpx
    import openai

    from backend.diagnosis.adaptive_tools import retryable_failure_category

    exc = openai.APITimeoutError(
        request=httpx.Request("POST", "https://endpoint.invalid/v1")
    )

    assert retryable_failure_category(exc) == FailureCategory.TIMEOUT


@pytest.mark.anyio
async def test_retry_coordinator_retries_model_behavior_error_once():
    from agents.exceptions import ModelBehaviorError

    from backend.diagnosis.adaptive_tools import RetryCoordinator

    calls = 0

    async def operation(attempt):
        nonlocal calls
        calls += 1
        if attempt == 1:
            raise ModelBehaviorError("bad json")
        return "ok"

    assert await RetryCoordinator(max_retries=1).run(operation) == "ok"
    assert calls == 2

    async def always_bad(attempt):
        raise ModelBehaviorError("bad json")

    with pytest.raises(ModelBehaviorError):
        await RetryCoordinator(max_retries=1).run(always_bad)


def test_bare_timeout_error_is_not_generically_retryable():
    # 工具路径对裸 TimeoutError 有显式不重试契约（SDK 取消语义）；模型调用超时
    # 在 v11_runtime 的 re-raise 处转换为 ClassifiedRetryableError(TIMEOUT)，
    # 不经过本函数。
    from backend.diagnosis.adaptive_tools import retryable_failure_category

    assert retryable_failure_category(TimeoutError()) is None


def test_model_retryable_exception_converts_timeout_only():
    # 模型调用超时是网关慢/长生成的运营噪声：转换为可重试的 TIMEOUT；
    # 其他异常原样透传，不改变既有分类。
    from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
    from backend.diagnosis.v11_runtime import _model_retryable_exception

    converted = _model_retryable_exception(TimeoutError("slow gateway"))
    assert isinstance(converted, ClassifiedRetryableError)
    assert converted.category == FailureCategory.TIMEOUT

    other = ValueError("nope")
    assert _model_retryable_exception(other) is other


def test_retry_coordinator_requires_cap_when_backoff_base_set():
    # 复审 L1：cap 缺省为 0 会让 min(cap, ...) 恒为 0，静默关闭退避；
    # 构造侧 fail fast。
    from backend.diagnosis.adaptive_tools import RetryCoordinator

    with pytest.raises(ValueError, match="backoff_cap"):
        RetryCoordinator(backoff_base_seconds=5.0)

    coordinator = RetryCoordinator(backoff_base_seconds=0.0)
    assert coordinator.backoff_cap_seconds == 0.0


@pytest.mark.anyio
async def test_retry_coordinator_applies_bounded_exponential_backoff(monkeypatch):
    from backend.diagnosis.adaptive_tools import (
        ClassifiedRetryableError,
        RetryCoordinator,
    )

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = 0

    async def operation(attempt):
        nonlocal calls
        calls += 1
        if attempt <= 3:
            raise ClassifiedRetryableError(FailureCategory.TIMEOUT)
        return "ok"

    result = await RetryCoordinator(
        max_retries=3, backoff_base_seconds=5.0, backoff_cap_seconds=30.0
    ).run(operation)

    assert result == "ok"
    assert calls == 4
    assert sleeps == [5.0, 10.0, 20.0]


@pytest.mark.anyio
async def test_retry_coordinator_backoff_cap_and_default_off(monkeypatch):
    from backend.diagnosis.adaptive_tools import (
        ClassifiedRetryableError,
        RetryCoordinator,
    )

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def operation(attempt):
        raise ClassifiedRetryableError(FailureCategory.TIMEOUT)

    with pytest.raises(ClassifiedRetryableError):
        await RetryCoordinator(
            max_retries=3, backoff_base_seconds=100.0, backoff_cap_seconds=7.0
        ).run(operation)
    assert sleeps == [7.0, 7.0, 7.0]

    sleeps.clear()

    async def failing(attempt):
        raise ClassifiedRetryableError(FailureCategory.TIMEOUT)

    with pytest.raises(ClassifiedRetryableError):
        await RetryCoordinator(max_retries=1).run(failing)
    assert sleeps == []



def test_tool_pages_and_accumulated_history_preserve_omission_and_call_pairs():
    from backend.diagnosis.adaptive_tools import _response, compact_tool_history

    evidence = [{"id": f"ev-{i}", "summary": "signal " * 70} for i in range(100)]
    response = _response(
        status=ToolCallStatus.SUCCESS, evidence=evidence, warning="provider partial",
        stop_reason=None, truncated=True,
    )
    page = json.loads(response)
    assert len(response) <= 6000
    assert len(evidence) == 100
    assert page["truncated"] and page["returned_count"] + page["omitted_count"] == 100
    assert page["total_count"] is None
    history = []
    for i in range(3):
        history.extend([
            {"type": "function_call", "name": "read_logs", "call_id": str(i), "arguments": "{}"},
            {"type": "function_call_output", "call_id": str(i), "output": response},
        ])
    original = json.dumps(history)
    projected = compact_tool_history(history)
    results = [item for item in projected if item["type"] == "function_call_output"]
    assert sum(len(item["output"]) for item in results) <= 6000
    for before, after in zip(history, projected, strict=True):
        assert before["call_id"] == after["call_id"]
        if after["type"] == "function_call_output":
            payload = json.loads(after["output"])
            assert payload["warning"] == "provider partial"
            assert payload["retrieval_hint"] and payload["truncated"]
        else:
            assert before == after
    assert json.dumps(history) == original
    small_history = [
        {"type": "function_call_output", "call_id": "small", "output": json.dumps({
            "status": "success", "evidence": evidence[:1], "returned_count": 1,
            "truncated": False, "stop_reason": None,
        })}
    ]
    assert compact_tool_history(small_history) is small_history


def test_metric_page_compacts_details_before_dropping_mechanism_controls():
    from backend.diagnosis.adaptive_tools import _bounded_tool_response

    evidence = [{
        "id": f"ev-{family}", "metric": f"service_{family}",
        "scope_entity_ids": ["service"],
        "baseline_mean": 1, "observation_mean": 2, "summary": "sample " * 150,
        "time_profile": [{"start": "2026-01-01", "mean": 2}] * 8,
        "related_metric_names": ["service_system_cpu"],
        "related_observations": [{"metric": "service_system_cpu", "baseline_mean": 1,
                                  "observation_mean": 12}],
    } for family in ("cpu", "memory", "socket", "disk_io")]
    payload = {"evidence": evidence, "returned_count": 4, "truncated": False}
    original = json.dumps(payload)
    first = _bounded_tool_response(payload, 2000)
    result = json.loads(first)
    assert len(first) <= 2000
    assert [item["id"] for item in result["evidence"]] == [item["id"] for item in evidence]
    for item in result["evidence"]:
        assert item["related_observations"] == [["service_system_cpu", 1, 12, None]]
        assert item["omitted_detail_fields"] == ["time_profile", "related_metric_names", "summary"]
    # 累计历史会再次经过压缩；列式细项不能在第二次压缩时被当作无效对象清空。
    second = json.loads(_bounded_tool_response(result, len(first) - 1))
    assert second["evidence"][0]["related_observations"] == [["service_system_cpu", 1, 12, None]]
    assert json.dumps(payload) == original


@pytest.mark.anyio
async def test_unavailable_tools_are_hidden_and_rejections_do_not_charge_budget():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "new-evidence")
    session = _session([provider], per_agent=4)
    assert [tool.name for tool in session.tools_for(AgentName.LOG, 1)] == ["read_logs"]
    rejected = json.loads(await session.invoke(AgentName.LOG, "read_logs", "{}invalid", 1))
    assert rejected["new_evidence_count"] == 0
    assert session.tool_calls[-1].budget_charged is False
    result = json.loads(await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload()), 1,
    ))
    assert result["remaining_tool_calls"] == 2
    assert result["new_evidence_count"] == 1
    assert session.tool_calls[-1].budget_charged is True
    duplicate = json.loads(await session.invoke(
        AgentName.LOG, "read_logs", json.dumps(_query_payload()), 1,
    ))
    assert duplicate["new_evidence_count"] == 0
    assert session.tool_calls[-1].budget_charged is False


def test_compacted_trace_keeps_rpc_error_instead_of_only_parent_wait():
    from backend.diagnosis.adaptive_tools import _bounded_tool_response, project_trace_details

    spans = [EvidenceItem(
        id=f"parent-{i}", provider=EvidenceProvider.TRACE, kind=EvidenceKind.TRACE_LATENCY,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), summary="long parent request " * 100,
        payload={"trace_id": f"trace-{i}", "span_id": f"root-{i}", "service": "gateway",
                 "operation": "request", "duration_ms": 17000, "status": "unset"},
    ) for i in range(8)]
    failed = spans[0].model_copy(update={
        "id": "failed-rpc", "kind": EvidenceKind.TRACE_ERROR,
        "payload": {"trace_id": "trace-0", "span_id": "rpc-0", "parent_span_id": "root-0",
                    "service": "gateway", "operation": "GetItems", "duration_ms": 16900,
                    "status": "error", "attributes": {"rpc.status_code": "14"}},
    })
    projection = project_trace_details(failed)
    assert projection["span_status"] == "error"
    assert projection["duration_ms"] == 16900
    original = {"status": "success", "evidence": project_tool_evidence([*spans, failed]),
                "returned_count": 9, "truncated": False, "stop_reason": None}
    response = _bounded_tool_response(original, max_chars=1000)
    assert len(response) <= 1000
    result = json.loads(response)
    assert result["evidence"][0]["id"] == failed.id
    assert result["evidence"][0]["status"] == "success"
    assert result["evidence"][0]["span_status"] == "error"
    assert result["evidence"][0]["trace_details"]["rpc.status_code"] == "14"
    assert result["truncated"]
    assert original["evidence"][-1]["id"] == failed.id


@pytest.mark.anyio
async def test_sdk_tool_deadline_does_not_cancel_successful_provider_commit():
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, "ev-log")
    persisted = []

    async def persist_start(call):
        await asyncio.sleep(0.6)
        persisted.append(call)
        return call

    async def persist_result(result):
        await asyncio.sleep(0.6)
        persisted.append(result.call)
        return result.call

    session = AdaptiveToolSession(
        event=_event(), seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([provider])),
        task_ids=_task_ids(), tool_timeout_seconds=0.05,
        persist_tool_start=persist_start, persist_tool_result=persist_result,
    )
    tool = next(t for t in session.tools_for(AgentName.LOG, 1) if t.name == "read_logs")
    response = json.loads(await asyncio.wait_for(
        tool.on_invoke_tool(None, json.dumps(_query_payload())), timeout=tool.timeout_seconds,
    ))
    assert provider.calls == 1
    assert response["status"] == "success"
    assert response["evidence"][0]["id"] == "ev-log"
    assert [call.status for call in persisted] == [ToolCallStatus.RUNNING, ToolCallStatus.SUCCESS]
    assert session.tool_timeout_seconds == 0.05


def test_trace_projection_omits_invalid_status_and_nonfinite_duration():
    from backend.diagnosis.adaptive_tools import project_trace_details

    evidence = EvidenceItem(
        id="malformed-trace", provider=EvidenceProvider.TRACE, kind=EvidenceKind.TRACE_LATENCY,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), summary="untrusted trace",
        payload={"status": [], "duration_ms": -1, "child_timing": "invalid",
                 "attributes": "invalid"},
    )
    projection = project_trace_details(evidence)
    assert "span_status" not in projection
    assert "duration_ms" not in projection
    assert "child_timing" not in projection


@pytest.mark.parametrize("role", ["caller", "callee"])
def test_trace_pair_projection_names_client_and_server_separately(role):
    from backend.diagnosis.adaptive_tools import project_trace_details

    local_client = role == "callee"
    evidence = EvidenceItem(
        id="paired-trace", provider=EvidenceProvider.TRACE, kind=EvidenceKind.TRACE_LATENCY,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), summary="paired observation",
        payload={"service": "worker" if local_client else "storage",
                 "duration_ms": 400 if local_client else 0.02, "status": "ok",
                 "attributes": {"rpc.peer_role": role,
                                "rpc.peer_service": "storage" if local_client else "worker",
                                "rpc.peer_duration_ms": "0.02" if local_client else "400"}},
    )
    pair = project_trace_details(evidence)["rpc_pair"]
    assert "caller resource stalls" in pair.pop("semantics")
    assert pair == {
        "client_service": "worker", "server_service": "storage",
        "client_duration_ms": 400, "server_duration_ms": 0.02,
    }


def test_trace_page_shares_explanations_without_dropping_paired_observations():
    from backend.diagnosis.adaptive_tools import _bounded_tool_response, project_trace_details

    evidence = [EvidenceItem(
        id=f"paired-{i}", provider=EvidenceProvider.TRACE, kind=EvidenceKind.TRACE_LATENCY,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), summary="paired observation",
        payload={"service": "worker", "operation": "Read", "duration_ms": 400 + i,
                 "status": "ok", "attributes": {"rpc.peer_role": "callee",
                 "rpc.peer_service": "storage", "rpc.peer_duration_ms": "0.02"}},
    ) for i in range(12)]
    payload = {"status": "success", "evidence": project_tool_evidence(evidence),
               "returned_count": 12, "truncated": False, "stop_reason": None}
    original = json.dumps(payload)
    rendered = _bounded_tool_response(payload)
    page = json.loads(rendered)
    assert len(rendered) <= 6000
    assert {item["id"] for item in page["evidence"]} == {item.id for item in evidence}
    assert page["trace_semantics"]["rpc_pair"] == (
        project_trace_details(evidence[0])["rpc_pair"]["semantics"]
    )
    assert rendered.count("caller resource stalls") == 1
    assert all(item["rpc_pair"]["server_duration_ms"] == 0.02 for item in page["evidence"])
    assert json.dumps(payload) == original
    assert json.loads(_bounded_tool_response(page)) == page


def test_trace_page_keeps_population_and_both_rpc_outcomes_when_bounded():
    from backend.diagnosis.adaptive_tools import _bounded_tool_response

    stats = {"service": "caller", "operation": "shop.API/Read", "matching_count": 100,
             "scan_complete": True, "filters": {"error_only": False},
             "rows": [["start", "end", "ok", 90, 10, 10, 10, 10],
                      ["start", "end", "error", 10, 2000, 2000, 2000, 2000]]}
    spans = [{"id": f"span-{i}", "provider": "trace", "service": "caller",
              "operation": "shop.API/Read", "span_status": "error" if i < 10 else "ok",
              "duration_ms": 2000 if i < 10 else 10, "summary": "repeat " * 100,
              **({"query_population": stats} if i == 0 else {})} for i in range(20)]
    page = json.loads(_bounded_tool_response({"evidence": spans}, max_chars=2400))
    assert page["evidence"][0]["query_population"] == stats
    assert {item["span_status"] for item in page["evidence"]} == {"ok", "error"}


def test_trace_order_retains_duration_contrasts_and_is_stable():
    from backend.diagnosis.adaptive_tools import order_trace_evidence

    spans = [{"id": f"trace-{i}", "provider": "trace", "service": "worker",
              "operation": "rpc/Read", "span_status": "ok", "duration_ms": duration}
             for i, duration in enumerate([9000, 8000, 7000, 10, 5])]
    metric = {"id": "metric-1", "provider": "metric"}
    ordered = order_trace_evidence([metric, *spans])
    assert ordered[0] == metric
    assert [item["duration_ms"] for item in ordered[1:3]] == [9000, 5]
    assert {item["id"] for item in ordered} == {item["id"] for item in [metric, *spans]}
    assert order_trace_evidence([metric, *reversed(spans)]) == ordered
    failed = {**spans[0], "id": "error-1", "span_status": "error", "duration_ms": 17000}
    contrasted = order_trace_evidence([*spans, failed])
    assert [(item["span_status"], item["duration_ms"]) for item in contrasted[:2]] == [
        ("error", 17000), ("ok", 5),
    ]


def test_tool_history_reuses_empty_page_space_for_success_failure_contrast():
    from backend.diagnosis.adaptive_tools import compact_tool_history

    spans = [{"id": f"span-{i}", "provider": "trace", "service": "worker",
              "operation": "rpc/Read", "span_status": status, "duration_ms": duration,
              "trace_id": f"{i:032x}", "summary": "repeated details " * 100}
             for i, (status, duration) in enumerate([("error", 17000), ("ok", 5)] * 10)]
    history = [{"type": "function_call_output", "call_id": f"call-{i}",
                "output": json.dumps({"status": "success", "evidence": spans if i == 2 else [],
                                      "returned_count": len(spans) if i == 2 else 0,
                                      "truncated": i == 2, "stop_reason": None})}
               for i in range(8)]
    original = json.dumps(history)
    result = compact_tool_history(history)
    assert sum(len(item["output"]) for item in result) <= 6000
    assert [item["call_id"] for item in result] == [item["call_id"] for item in history]
    visible = json.loads(result[2]["output"])["evidence"]
    assert {item["span_status"] for item in visible} == {"ok", "error"}
    assert len(visible) >= 10
    assert json.dumps(history) == original
