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
        # 空证据成功会触发 no_new_evidence 停止该 agent；每个工具用独立身份。
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
    registry = build_provider_tool_registry(ProviderRegistry(providers))
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
