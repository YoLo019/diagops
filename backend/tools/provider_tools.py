from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from time import perf_counter

from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvenance,
    EvidenceProvider,
    EvidenceScope,
    EvidenceSourceClass,
    JsonValue,
    VerifiedIncidentPayload,
)
from backend.domain.memory import MemoryItem, MemoryVerificationStatus
from backend.domain.tool_calls import (
    ToolCallRecord,
    ToolCallStatus,
    ToolExposure,
    ToolSpec,
)
from backend.domain.tool_queries import (
    DependencyQuery,
    DeploymentQuery,
    LogQuery,
    MemoryQuery,
    MetricQuery,
    PrometheusQuery,
    RelatedAlertQuery,
    RuntimeStateQuery,
    ServiceCatalogQuery,
    TraceQuery,
)
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.safety.redaction import redact_text, redact_value, safe_failure
from backend.tools.registry import ToolInvocationResult, ToolRegistry

_PROVIDER_TOOLS: dict[str, EvidenceProvider] = {
    "read_logs": EvidenceProvider.LOG,
    "query_metrics": EvidenceProvider.METRIC,
    "read_deployments": EvidenceProvider.DEPLOY,
    "read_service_catalog": EvidenceProvider.SERVICE_CATALOG,
    "query_prometheus": EvidenceProvider.METRIC,
    "query_dependencies": EvidenceProvider.DEPENDENCY,
    "query_traces": EvidenceProvider.TRACE,
    "read_runtime_state": EvidenceProvider.RUNTIME_STATE,
    "query_related_alerts": EvidenceProvider.RELATED_ALERT,
}

# query_prometheus 仅为内部兼容别名，不出现在 V11 Agent manifest。
_INTERNAL_TOOLS = frozenset({"query_prometheus"})

_DESCRIPTIONS: dict[str, str] = {
    "read_logs": (
        "Read log evidence. Requires start_time, end_time, and reason; the window "
        "must be timezone-aware and no longer than two hours. All keywords must match "
        "the same record (AND); levels matches any listed level. Use instance to scope "
        "the entity and one keyword or [] before adding restrictive filters."
    ),
    "query_metrics": (
        "Read metric evidence. Requires start_time, end_time, and reason; the window "
        "must be timezone-aware and no longer than two hours."
    ),
    "read_deployments": (
        "Read deployment evidence. Requires start_time, end_time, and reason; the "
        "window must be timezone-aware and no longer than two hours."
    ),
    "read_service_catalog": (
        "Read service catalog evidence. Requires start_time, end_time, and reason; "
        "the window must be timezone-aware and no longer than two hours."
    ),
    "query_prometheus": (
        "Query Prometheus metric evidence. Requires start_time, end_time, and reason; "
        "the window must be timezone-aware and no longer than two hours."
    ),
    "query_dependencies": (
        "Read dependency evidence. Requires start_time, end_time, and reason; the "
        "window must be timezone-aware and no longer than two hours. direction is "
        "either upstream or downstream; depth is fixed at 1."
    ),
    "query_traces": (
        "Read bounded trace span evidence. Use window_start and window_end together "
        "when a time window is needed; do not use start_time or end_time."
    ),
    "read_runtime_state": (
        "Read runtime state evidence. Use window_start and window_end together when "
        "a time window is needed; do not use start_time or end_time. states accepts "
        "at most 8 values."
    ),
    "query_related_alerts": (
        "Read related alert evidence. Use window_start and window_end together when "
        "a time window is needed; do not use start_time or end_time."
    ),
    "lookup_memory": "Lookup verified prior incident memory; no time-window fields are used.",
}

QUERY_MODELS_BY_TOOL = {
    "read_logs": LogQuery,
    "query_metrics": MetricQuery,
    "read_deployments": DeploymentQuery,
    "read_service_catalog": ServiceCatalogQuery,
    "query_prometheus": PrometheusQuery,
    "query_dependencies": DependencyQuery,
    "query_traces": TraceQuery,
    "read_runtime_state": RuntimeStateQuery,
    "query_related_alerts": RelatedAlertQuery,
    "lookup_memory": MemoryQuery,
}

_CURRENT_INVESTIGATION_ID: ContextVar[str | None] = ContextVar(
    "diagops_current_investigation_id", default=None
)


def current_investigation_id() -> str | None:
    """返回当前异步诊断上下文的 investigation owner。"""
    return _CURRENT_INVESTIGATION_ID.get()


@contextmanager
def current_investigation_scope(investigation_id: str) -> Iterator[None]:
    """为 Provider 查询绑定当前 investigation，退出时恢复外层上下文。"""
    token = _CURRENT_INVESTIGATION_ID.set(investigation_id)
    try:
        yield
    finally:
        _CURRENT_INVESTIGATION_ID.reset(token)


def build_provider_tool_registry(
    provider_registry: ProviderRegistry,
    memory_lookup: "VerifiedMemoryLookup | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    provider_handler = partial(invoke_provider_tool, provider_registry)
    for name, provider in _PROVIDER_TOOLS.items():
        registry.register(
            ToolSpec(
                name=name,
                description=_DESCRIPTIONS[name],
                input_schema=QUERY_MODELS_BY_TOOL[name].model_json_schema(),
                read_only=True,
                provider=provider,
                exposure=(
                    ToolExposure.INTERNAL if name in _INTERNAL_TOOLS else ToolExposure.AGENT
                ),
            ),
            provider_handler,
            available=partial(provider_registry.supports_tool, name),
        )
    registry.register(
        ToolSpec(
            name="lookup_memory",
            description=_DESCRIPTIONS["lookup_memory"],
            input_schema=MemoryQuery.model_json_schema(),
            read_only=True,
            provider=EvidenceProvider.VERIFIED_INCIDENT,
        ),
        partial(invoke_lookup_memory, memory_lookup),
        available=lambda _event: memory_lookup is not None,
    )
    registry.verified_memory_lookup = memory_lookup
    return registry


def invoke_provider_tool(
    provider_registry: ProviderRegistry,
    *,
    tool_name: str,
    event: IncidentEvent,
    task_id: str,
    agent_name: str,
    input: dict[str, JsonValue] | None = None,
) -> ToolInvocationResult:
    provider = _PROVIDER_TOOLS[tool_name]
    started_at = datetime.now(UTC)
    started = perf_counter()
    try:
        if input is None:
            target_providers = [
                item
                for item in provider_registry.providers
                if getattr(item, "provider", EvidenceProvider.LOG) == provider
            ]
            results = [
                result
                for result in ProviderRegistry(target_providers).collect_results(event)
                if result.provider == provider
            ]
            record_input = None
        else:
            query = QUERY_MODELS_BY_TOOL[tool_name].model_validate(input)
            results = provider_registry.query_results(event, tool_name, query)
            record_input = query.model_dump(mode="json")
        call = _record(
            tool_name=tool_name,
            task_id=task_id,
            agent_name=agent_name,
            input=record_input,
            started_at=started_at,
            started=started,
            status=_tool_status(results),
            evidence_ids=[
                item.id for result in results for item in result.evidence_items
            ],
            error_message=_result_message(results),
        )
        return ToolInvocationResult(
            call=call,
            evidence=provider_registry.evidence_from_results(results),
            provider_results=results,
        )
    except Exception:
        call = _record(
            tool_name=tool_name,
            task_id=task_id,
            agent_name=agent_name,
            input=redact_value(input) if input else None,
            started_at=started_at,
            started=started,
            status=ToolCallStatus.FAILED,
            evidence_ids=[],
            error_message=safe_failure("invalid_output"),
        )
        return ToolInvocationResult(call=call, evidence=[], provider_results=[])


def invoke_lookup_memory(
    memory_lookup: "VerifiedMemoryLookup | None",
    *,
    tool_name: str,
    event: IncidentEvent,
    task_id: str,
    agent_name: str,
    input: dict[str, JsonValue] | None = None,
) -> ToolInvocationResult:
    started_at = datetime.now(UTC)
    started = perf_counter()
    query: MemoryQuery | None = None
    record_input: dict[str, JsonValue] | None = None
    if input is not None:
        try:
            query = MemoryQuery.model_validate(input)
            record_input = query.model_dump(mode="json")
        except (TypeError, ValueError):
            call = _record(
                tool_name=tool_name,
                task_id=task_id,
                agent_name=agent_name,
                input=redact_value(input),
                started_at=started_at,
                started=started,
                status=ToolCallStatus.FAILED,
                evidence_ids=[],
                error_message=safe_failure("invalid_output"),
            )
            return ToolInvocationResult(call=call, evidence=[], provider_results=[])
    # 未配置 verified-memory store 或无可引用记录时，空结果是显式的 success。
    evidence = memory_lookup.lookup(event, query) if memory_lookup is not None else []
    call = _record(
        tool_name=tool_name,
        task_id=task_id,
        agent_name=agent_name,
        input=record_input,
        started_at=started_at,
        started=started,
        status=ToolCallStatus.SUCCESS,
        evidence_ids=[item.id for item in evidence],
        error_message=None,
    )
    return ToolInvocationResult(call=call, evidence=evidence, provider_results=[])


class VerifiedMemoryLookup:
    """只从受 guard 的 repository 记录产出 verified memory 证据。

    不变量（spec 8.1）：只引用 verification_status=verified 且来源
    investigation/candidate 仍存在的记录；created_at/verified_at 晚于当前事件
    started_at 的记录不可引用；当前 investigation 及其 source 祖先的记录
    永远被拒绝，防止 rerun 读回自己冻结过的答案。
    """

    def __init__(
        self,
        repository,
        *,
        adapter_version: str = "verified-memory-v1",
        current_investigation_id: Callable[[], str | None] | None = None,
    ) -> None:
        self._repository = repository
        self._adapter_version = adapter_version
        # IncidentEvent 契约不带 investigation 身份；由宿主注入 resolver。
        self._current_investigation_id = current_investigation_id or (lambda: None)

    def lookup(
        self, event: IncidentEvent, query: MemoryQuery | None = None
    ) -> list[EvidenceItem]:
        limit = query.limit if query is not None else 5
        candidates = self._repository.list_memory(event.service, event.environment)
        excluded_sources = self._self_and_ancestor_ids(event)
        evidence: list[EvidenceItem] = []
        for item in candidates:
            if len(evidence) >= limit:
                break
            projection = self._eligible_projection(item, event, excluded_sources)
            if projection is None:
                continue
            candidate, source_id = projection
            if query is not None and not self._matches_filters(candidate, query):
                continue
            evidence.append(self._to_evidence(item, candidate, source_id, event))
        return evidence

    def _eligible_projection(
        self,
        item: MemoryItem,
        event: IncidentEvent,
        excluded_sources: frozenset[str],
    ) -> tuple[object | None, str] | None:
        if item.verification_status != MemoryVerificationStatus.VERIFIED:
            return None
        if item.source_investigation_id is None or item.root_candidate_id is None:
            return None
        if item.verified_at is None:
            return None
        cutoff = event.started_at
        if item.created_at > cutoff or item.verified_at > cutoff:
            return None
        source_id = item.source_investigation_id
        if source_id in excluded_sources:
            return None
        try:
            self._repository.get(source_id)
            review = self._repository.get_coordination_review(source_id)
        except (KeyError, ValueError):
            return None
        if review is None:
            return None
        candidate = next(
            (
                entry
                for entry in getattr(review, "candidates", [])
                if entry.id == item.root_candidate_id
            ),
            None,
        )
        if candidate is None:
            return None
        return candidate, source_id

    def _self_and_ancestor_ids(self, event: IncidentEvent) -> frozenset[str]:
        current_id = (
            self._current_investigation_id()
            or getattr(event, "investigation_id", None)
        )
        if not current_id:
            return frozenset()
        lineage: set[str] = {current_id}
        try:
            record = self._repository.get(current_id)
        except (KeyError, ValueError):
            return frozenset(lineage)
        # 沿 source_investigation_id 链向上，环防御限制在 32 跳内。
        for _ in range(32):
            parent_id = getattr(record, "source_investigation_id", None)
            if not parent_id or parent_id in lineage:
                break
            lineage.add(parent_id)
            try:
                record = self._repository.get(parent_id)
            except (KeyError, ValueError):
                break
        return frozenset(lineage)

    @staticmethod
    def _matches_filters(candidate: object, query: MemoryQuery) -> bool:
        if query.affected_entity is not None and (
            getattr(candidate, "affected_entity", None) != query.affected_entity
        ):
            return False
        if query.failure_mechanism is not None and (
            getattr(candidate, "failure_mechanism", None) != query.failure_mechanism
        ):
            return False
        return True

    def _to_evidence(
        self,
        item: MemoryItem,
        candidate: object,
        source_id: str,
        event: IncidentEvent,
    ) -> EvidenceItem:
        assert item.verified_at is not None and item.root_candidate_id is not None
        payload = VerifiedIncidentPayload(
            source_investigation_id=source_id,
            root_candidate_id=item.root_candidate_id,
            verified_at=item.verified_at,
            summary=redact_text(item.summary)[:512],
            service=event.service,
            environment=event.environment,
            affected_entity=getattr(candidate, "affected_entity", None),
            failure_mechanism=getattr(candidate, "failure_mechanism", None),
        )
        entity_ids = (
            [payload.affected_entity] if payload.affected_entity is not None else []
        )
        return EvidenceItem(
            provider=EvidenceProvider.VERIFIED_INCIDENT,
            kind=EvidenceKind.VERIFIED_INCIDENT,
            timestamp=item.verified_at,
            summary=f"verified memory: {payload.summary[:120]}",
            payload=payload.model_dump(mode="json"),
            confidence=1.0,
            scope=EvidenceScope(
                entity_ids=entity_ids,
                observed_at=item.verified_at,
                signal_type="verified_incident",
            ),
            provenance=EvidenceProvenance(
                source_class=EvidenceSourceClass.RECORDED_LOCAL,
                provider_profile="verified_memory",
                source_artifact_id=item.id,
                adapter_version=self._adapter_version,
            ),
            runtime_run_id=getattr(event, "runtime_run_id", None),
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


def _tool_status(results: list[ProviderResult]) -> ToolCallStatus:
    if any(result.status == ProviderStatus.FAILED for result in results):
        return ToolCallStatus.FAILED
    if results and all(result.status == ProviderStatus.SKIPPED for result in results):
        return ToolCallStatus.SKIPPED
    return ToolCallStatus.SUCCESS
