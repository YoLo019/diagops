"""V11 Agent authority runtime 的 RED→GREEN 契约测试。"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import openai
import pytest
from agents import FunctionTool, Model, ModelResponse, ModelSettings, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel, ConfigDict

from backend.config.settings import (
    AgentsSettings,
    AppSettings,
    StorageSettings,
)
from backend.config.settings import (
    endpoint_id as endpoint_identity,
)
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis import v11_runtime as v11_runtime_module
from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
from backend.diagnosis.diagnostic_skills import (
    DIAGNOSTIC_SKILLS,
    SKILL_CATALOG_VERSION,
    skill_catalog_hash,
)
from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
)
from backend.diagnosis.openai_model import OFFICIAL_OPENAI_BASE_URL
from backend.diagnosis.v11_runtime import (
    LeadPlanningOutput,
    V11Runtime,
    V11RuntimeContractError,
    _V11BudgetedModel,
)
from backend.domain.agent_findings import (
    AgentFindingType,
    CausalCheck,
    CausalCheckName,
    CoordinationReview,
    CriticAssessment,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadDecision,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import (
    CausalCheckStatus,
    CriticVerdict,
    ExecutionStepKind,
    FailureCategory,
    LeadAction,
    ModelProvider,
)
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunReason,
    seal_v11_execution_contract,
)
from backend.domain.tool_calls import ToolSpec
from backend.providers.registry import build_mock_provider_registry
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import V11_PHASE_ORDER, PhaseInput
from backend.services.container import AppContainer
from backend.tools.provider_tools import (
    VerifiedMemoryLookup,
    build_provider_tool_registry,
    current_investigation_id,
    current_investigation_scope,
)
from backend.tools.registry import agent_manifest_hash


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="checkout errors",
        description="elevated checkout failures",
        started_at=datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
    )


def _repository() -> tuple[InMemoryInvestigationRepository, InvestigationRecord]:
    repository = InMemoryInvestigationRepository()
    record = repository.save(InvestigationRecord(id="inv-v11", event=_event()))
    return repository, record


def _seed_repository() -> tuple[InMemoryInvestigationRepository, InvestigationRecord]:
    repository, record = _repository()
    return repository, repository.save(
        record.model_copy(
            update={
                "evidence": [
                    EvidenceItem(
                        id="ev-metric",
                        provider=EvidenceProvider.METRIC,
                        kind=EvidenceKind.METRIC_TREND,
                        timestamp=record.event.started_at,
                        summary="error rate increased",
                        runtime_run_id="run-v11",
                    )
                ]
            }
        )
    )


def _complete_contract(
    registry,
    *,
    manifest: tuple[str, ...] | None = None,
    model_name: str = "fake",
    model_provider: str = "openai",
    api_mode: str = "responses",
    endpoint_id: str | None = None,
    artifact_hash: str | None = None,
) -> dict:
    frozen_manifest = manifest or registry.agent_manifest()
    frozen_endpoint_id = endpoint_id
    if model_provider == "openai" and frozen_endpoint_id is None:
        frozen_endpoint_id = endpoint_identity(OFFICIAL_OPENAI_BASE_URL)
    return seal_v11_execution_contract(
        {
            "execution_contract_version": "v11",
            "authority_mode": "agent",
            "model_provider": model_provider,
            "model_name": model_name,
            "prompt_version": "v11-test",
            "api_mode": api_mode,
            "endpoint_id": frozen_endpoint_id,
            "capability_artifact_hash": artifact_hash,
            "tool_manifest": list(frozen_manifest),
            "tool_manifest_hash": agent_manifest_hash(frozen_manifest),
            "skill_catalog": {
                "catalog_version": SKILL_CATALOG_VERSION,
                "catalog_hash": skill_catalog_hash(DIAGNOSTIC_SKILLS),
                "skill_names": ",".join(
                    f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS
                ),
            },
            "capability_identity": {
                "provider": model_provider,
                "model": model_name,
                "api_mode": api_mode,
                "endpoint_id": frozen_endpoint_id,
                "artifact_hash": artifact_hash,
            },
            "limits": {
                "max_turns": 8,
                "max_investigators": 3,
                "max_rounds": 2,
                "token_budget": 10000,
                "max_tool_calls_per_specialist": 3,
                "tool_timeout_seconds": 10,
            },
            "retry_policy": {
                "max_retries": 1,
                "retryable_categories": ["transport", "rate_limit"],
                "provider_max_retries": 0,
                "sdk_max_retries": 0,
            },
            "tool_budget": 8,
            "token_budget": 10000,
            "timeout_seconds": 60.0,
        }
    )


@pytest.mark.anyio
async def test_lead_planning_persists_bounded_owned_tasks_before_investigation():
    repository, record = _repository()
    calls: list[dict] = []

    async def turn(**kwargs):
        calls.append(kwargs)
        assert kwargs["actor"] == "LeadAgent"
        return {
            "decision": {
                "action": "investigate",
                "summary": "Close the highest-value evidence gap.",
                "task_ids": ["task-timeline"],
                "selected_skills": ["first_failure_timeline@1.0.0"],
            },
            "tasks": [
                {
                    "id": "task-timeline",
                    "title": "Find the first failure",
                    "description": "Align the earliest error across signals.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs", "query_metrics"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                    "information_gap": "earliest causal boundary",
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    plan = await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )

    assert plan.runtime_run_id == "run-v11"
    assert plan.lead_decision is not None
    assert plan.lead_decision.action == LeadAction.INVESTIGATE
    assert [task.id for task in plan.tasks] == ["task-timeline"]
    assert repository.get_plan(record.id).model_dump(mode="json") == plan.model_dump(
        mode="json"
    )
    assert calls[0]["remaining_tool_budget"] == 8
    assert 0 < calls[0]["remaining_token_budget"] <= 1000


@pytest.mark.anyio
async def test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations():
    repository, record = _repository()
    other = repository.save(
        InvestigationRecord(id="inv-other", event=_event())
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "investigate",
                "summary": "collect one bounded signal",
                "task_ids": ["task-target"],
            },
            "tasks": [
                {
                    "id": "task-target",
                    "title": "Inspect target",
                    "description": "Inspect the target investigation only.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-target",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )

    assert repository.get_plan(record.id) is not None
    assert repository.get_plan(other.id) is None
    assert [item.runtime_run_id for item in repository.list_executions(record.id)] == [
        "run-target"
    ]
    assert repository.list_executions(other.id) == []


@pytest.mark.anyio
async def test_v11_model_turn_budget_is_shared_by_multiple_model_invocations():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    remaining = [1]

    async def reserve_turn() -> int:
        if remaining[0] <= 0:
            raise V11RuntimeContractError("V11 model turn budget exhausted")
        remaining[0] -= 1
        return remaining[0]

    async def turn(**_kwargs):
        return {"value": "ok"}

    class Output(BaseModel):
        value: str

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=registry,
        turn=turn,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-v11-turn-budget",
            attempt_id="attempt-v11-turn-budget",
            phase=RuntimePhase.LEAD_PLANNING,
            resume_state=RuntimeResumeState(
                remaining_model_turns=1,
                remaining_token_budget=1000,
            ),
            execution_contract_version="v11",
            execution_contract=_complete_contract(registry),
            model_provider=ModelProvider.OPENAI,
            model_name="fake",
            reserve_model_turn=reserve_turn,
        )
    )
    kwargs = {
        "actor": "LeadAgent",
        "prompt": "bounded",
        "output_type": Output,
        "context": {},
        "tools": [],
        "remaining_token_budget": 1000,
    }
    await runtime._call_model(**kwargs)
    assert runtime.remaining_model_turns == 0
    with pytest.raises(V11RuntimeContractError, match="exhausted"):
        await runtime._call_model(**kwargs)


@pytest.mark.anyio
async def test_lead_planning_rejects_conclude_and_duplicate_or_infeasible_work():
    repository, record = _repository()

    async def conclude(**_kwargs):
        return {
            "decision": {
                "action": "conclude",
                "summary": "premature",
                "candidate_ids": ["candidate-1"],
            },
            "tasks": [],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=conclude,
    )

    with pytest.raises(V11RuntimeContractError, match="planning cannot conclude"):
        await runtime.plan_lead(
            repository=repository,
            investigation_id=record.id,
            event=record.event,
            runtime_run_id="run-v11",
            remaining_tool_budget=8,
            remaining_token_budget=1000,
        )


def test_production_memory_wiring_exposes_the_current_investigation_resolver():
    repository, record = _repository()
    lookup = VerifiedMemoryLookup(
        repository,
        current_investigation_id=current_investigation_id,
    )
    registry = build_provider_tool_registry(
        build_mock_provider_registry(), lookup
    )
    lookup = V11Runtime.memory_lookup_from_registry(registry)

    assert lookup._current_investigation_id() is None
    with current_investigation_scope(record.id):
        assert lookup._current_investigation_id() == record.id


def test_app_container_wires_memory_resolver_for_current_investigation():
    container = AppContainer(
        AppSettings(storage=StorageSettings(url="memory://"))
    )
    try:
        record = container.repository.save(
            InvestigationRecord(id="container-inv", event=_event())
        )
        lookup = V11Runtime.memory_lookup_from_registry(container.tool_registry)
        assert lookup._current_investigation_id() is None
        with current_investigation_scope(record.id):
            assert lookup._current_investigation_id() == record.id
    finally:
        container.close()


@pytest.mark.anyio
async def test_v11_tool_dispatch_rechecks_the_frozen_manifest():
    from backend.diagnosis.adaptive_tools import AdaptiveToolSession

    repository, record = _repository()
    registry = build_provider_tool_registry(build_mock_provider_registry())
    manifest = registry.agent_manifest()
    observed: list[tuple[str, tuple[str, ...]]] = []
    original = registry.assert_agent_callable

    def assert_callable(tool_name: str, frozen_manifest: tuple[str, ...]):
        observed.append((tool_name, frozen_manifest))
        return original(tool_name, frozen_manifest)

    registry.assert_agent_callable = assert_callable
    session = AdaptiveToolSession(
        event=record.event,
        seed_evidence=[],
        registry=registry,
        task_ids={"investigator-1": "task-timeline"},
        runtime_run_id="run-v11",
        agent_manifest=manifest,
    )

    payload = {
        "reason": "find first failure",
        "start_time": "2026-08-08T07:00:00+00:00",
        "end_time": "2026-08-08T09:00:00+00:00",
        "keywords": ["error"],
        "levels": ["error"],
    }
    await session.invoke("investigator-1", "read_logs", json.dumps(payload), 1)
    await session.invoke(
        "investigator-1",
        "read_logs",
        json.dumps({**payload, "keywords": ["different"]}),
        1,
        attempt=2,
    )

    assert observed == [("read_logs", manifest), ("read_logs", manifest)]


def test_v11_frozen_manifest_rejects_same_cardinality_tool_swap():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    frozen = registry.agent_manifest()
    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=registry,
        turn=lambda **_kwargs: None,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-v11",
            attempt_id="attempt-v11",
            phase="lead_planning",
            resume_state=RuntimeResumeState(),
            execution_contract=_complete_contract(registry, manifest=frozen),
            model_provider=ModelProvider.OPENAI,
            model_name="fake",
        )
    )
    removed = frozen[0]
    registry._specs.pop(removed)
    registry._handlers.pop(removed)
    registry.register(
        ToolSpec(
            name="replacement_tool",
            description="replacement",
        ),
        lambda **_kwargs: None,
    )

    with pytest.raises(V11RuntimeContractError):
        runtime._agent_manifest()


def test_v11_frozen_manifest_rejects_unordered_contract():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    frozen = registry.agent_manifest()
    runtime = V11Runtime(
        model="fake", model_provider=ModelProvider.OPENAI, model_name="fake", tool_registry=registry
    )
    runtime._execution_contract = _complete_contract(
        registry, manifest=tuple(reversed(frozen))
    )

    with pytest.raises(V11RuntimeContractError, match="ordered"):
        runtime._agent_manifest()


def test_v11_nested_execution_contract_digest_fences_capability_and_limits():
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(enabled=True, model="gpt-test"),
        )
    )
    record = container.repository.save(
        InvestigationRecord(id="inv-contract-fence", event=_event())
    )
    run = container.create_runtime_run(
        record.id,
        strategy="adaptive",
        run_reason="initial",
        execution_contract_version="v11",
    )
    contract = json.loads(json.dumps(run.execution_contract))
    contract["capability_identity"]["artifact_hash"] = "f" * 64
    contract["limits"]["max_turns"] = 1

    with pytest.raises(V11RuntimeContractError, match="execution contract"):
        container.orchestrator.v11_runtime.clone_for_run(
            runtime_run_id=run.id,
            model_provider=run.model_provider,
            model_name=run.model_name,
            token_budget=run.token_budget,
            timeout_seconds=run.timeout_seconds,
            execution_contract=contract,
        )
    contract = json.loads(json.dumps(run.execution_contract))
    contract["limits"]["max_tool_calls_per_specialist"] = 5
    contract = seal_v11_execution_contract(contract)
    cloned = container.orchestrator.v11_runtime.clone_for_run(
        runtime_run_id=run.id,
        model_provider=run.model_provider,
        model_name=run.model_name,
        token_budget=run.token_budget,
        timeout_seconds=run.timeout_seconds,
        execution_contract=contract,
    )
    assert cloned.max_tool_calls_per_specialist == 5
    cloned._update_summary(container.repository, record.id)
    assert (
        container.repository.get(record.id).multi_agent_run.max_tool_calls_per_specialist
        == 5
    )
    container.close()


def test_v11_clone_disables_compatible_provider_retry_for_durable_run():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    source_model = OpenAICompatibleChatCompletionsModel(
        model="compat-model",
        api_key="local-secret",
        base_url="http://127.0.0.1:8000/v1",
    )
    runtime = V11Runtime(
        model=source_model,
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        tool_registry=registry,
    )
    contract = _complete_contract(
        registry,
        model_name="compat-model",
        model_provider="openai_compatible",
        api_mode="chat_completions",
        endpoint_id=endpoint_identity("http://127.0.0.1:8000/v1"),
        artifact_hash="a" * 64,
    )

    cloned = runtime.clone_for_run(
        runtime_run_id="run-provider-retry-fence",
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=10000,
        timeout_seconds=60,
        execution_contract=contract,
    )

    assert isinstance(cloned.model, OpenAICompatibleChatCompletionsModel)
    assert cloned.model._max_retries == 0


@pytest.mark.anyio
async def test_v11_string_provider_client_disables_sdk_transport_retry(monkeypatch):
    captured: dict[str, object] = {}

    class FakeClient:
        base_url = OFFICIAL_OPENAI_BASE_URL

        async def close(self) -> None:
            captured["closed"] = True

    def build_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(v11_runtime_module, "AsyncOpenAI", build_client)
    provider = v11_runtime_module._V11ModelProvider(
        V11Runtime(model="gpt-test")
    )

    assert captured["max_retries"] == 0
    await provider.aclose()
    assert captured["closed"] is True


@pytest.mark.anyio
async def test_v11_sdk_provider_retry_is_only_the_persisted_outer_retry(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            request=request,
            json={"error": {"message": "rate limited", "type": "rate_limit"}},
        )

    def build_client(**kwargs):
        return openai.AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(
        "backend.diagnosis.openai_compatible_model.AsyncOpenAI", build_client
    )
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=1000,
    )

    with pytest.raises(openai.RateLimitError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="bounded request",
            output_type=StrictOutput,
            context={"request": "bounded"},
            tools=[],
            remaining_token_budget=1000,
            remaining_tool_budget=8,
        )

    assert request_count == 2


@pytest.mark.anyio
async def test_v11_sdk_model_requests_reserve_decreasing_output_caps():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class TwoRequestModel(Model):
        def __init__(self) -> None:
            self.calls = 0
            self.output_caps: list[int | None] = []

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            del (
                system_instructions,
                input,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            )
            self.calls += 1
            self.output_caps.append(model_settings.max_tokens)
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        arguments="{}",
                        call_id="probe-call",
                        name="probe",
                        type="function_call",
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id="final-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return ModelResponse(
                output=output,
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id=f"response-{self.calls}",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    model = TwoRequestModel()

    async def probe(_context, _raw_input):
        return "probe result"

    tool = FunctionTool(
        name="probe",
        description="bounded probe",
        params_json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        on_invoke_tool=probe,
    )
    runtime = V11Runtime(model=model, token_budget=1000)

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded request",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[tool],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
    )

    assert model.calls == 2
    assert model.output_caps[1] < model.output_caps[0]


@pytest.mark.anyio
async def test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class ReplayModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            del (
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            )
            self.calls += 1
            if self.calls in {1, 3}:
                output = [
                    ResponseFunctionToolCall(
                        arguments="{}",
                        call_id=f"probe-call-{self.calls}",
                        name="probe",
                        type="function_call",
                    )
                ]
            elif self.calls == 2:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            else:
                output = [
                    ResponseOutputMessage(
                        id="final-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return ModelResponse(
                output=output,
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id=f"response-{self.calls}",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    async def probe(_context, _raw_input):
        return "probe result"

    tool = FunctionTool(
        name="probe",
        description="bounded probe",
        params_json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        on_invoke_tool=probe,
    )
    model = ReplayModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-sdk-usage-retry"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded replay request",
        output_type=StrictOutput,
        context={"request": "two provider requests"},
        tools=[tool],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-sdk-usage-retry",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    assert model.calls == 4
    started = [payload for status, payload in events if status == "started"]
    completed = [payload for status, payload in events if status == "completed"]
    retrying = [
        payload
        for status, payload in events
        if status == "failed" and payload.get("reservation_status") == "retrying"
    ]
    assert len(started) == 4
    assert len(completed) == 3
    assert len(retrying) == 1
    assert [payload["actual_input_tokens"] for payload in completed] == [20, 20, 20]
    first_request = started[0]["reservation_id"]
    failed_request = retrying[0]["reservation_id"]
    assert started[2]["reservation_id"] != first_request
    assert started[3]["reservation_id"] == failed_request
    assert failed_request != first_request
    assert runtime._model_reservations == {}
    assert runtime.remaining_token_budget == 1000 - sum(
        int(payload["input_tokens"]) + int(payload["output_tokens"])
        for payload in completed
    )
    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert summary.total_input_tokens == 60
    assert summary.total_output_tokens == 15
    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.runtime_run_id == runtime.runtime_run_id
    ]
    assert len(executions) == 2
    attempt_one = next(item for item in executions if item.attempt == 1)
    attempt_two = next(item for item in executions if item.attempt == 2)
    assert attempt_one.status == AgentExecutionStatus.FAILED
    assert attempt_one.input_tokens == 20 + int(retrying[0]["input_estimate"])
    assert attempt_one.output_tokens == 5
    assert attempt_two.status == AgentExecutionStatus.COMPLETED
    assert attempt_two.input_tokens == 40
    assert attempt_two.output_tokens == 10


@pytest.mark.anyio
async def test_v11_single_sdk_request_usage_is_not_counted_twice():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class SingleRequestModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="single-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id="single-response",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = SingleRequestModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-single-usage"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="single request usage",
        output_type=StrictOutput,
        context={"request": "one"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-single-usage",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert model.calls == 1
    assert summary.total_input_tokens == 20
    assert summary.total_output_tokens == 5
    completed = [payload for status, payload in events if status == "completed"]
    assert len(completed) == 1
    assert completed[0]["actual_input_tokens"] == 20
    execution = repository.list_executions(record.id)[0]
    assert execution.input_tokens == 20
    assert execution.output_tokens == 5


@pytest.mark.anyio
async def test_v11_all_failed_sdk_retry_records_estimate_without_summary_usage():
    class AlwaysFailModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            raise ClassifiedRetryableError(FailureCategory.TRANSPORT)

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = AlwaysFailModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-all-failed-usage"
    runtime._persist_model_event = persist_model_event

    with pytest.raises(ClassifiedRetryableError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="all failed usage",
            output_type=BaseModel,
            context={"request": "fail"},
            tools=[],
            remaining_token_budget=1000,
            remaining_tool_budget=8,
            repository=repository,
            investigation_id=record.id,
            task_id="task-all-failed-usage",
            step_kind=ExecutionStepKind.LEAD_PLANNING,
            analysis_round=1,
        )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert model.calls == 2
    assert summary.total_input_tokens == 0
    assert summary.total_output_tokens == 0
    assert runtime._input_tokens == 0
    assert runtime._output_tokens == 0
    assert runtime._model_reservations == {}
    assert not [status for status, _payload in events if status == "completed"]
    executions = repository.list_executions(record.id)
    assert len(executions) == 2
    assert all(item.status == AgentExecutionStatus.FAILED for item in executions)
    assert all(item.input_tokens > 0 for item in executions)
    assert all(item.output_tokens == 0 for item in executions)


@pytest.mark.anyio
async def test_v11_sdk_retry_resume_usage_is_idempotent():
    class RetryResumeModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="retry-resume-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id="retry-resume-response",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = RetryResumeModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-retry-resume-usage"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="retry resume usage",
        output_type=BaseModel,
        context={"request": "retry once"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-retry-resume-usage",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert model.calls == 2
    assert summary.total_input_tokens == 20
    assert summary.total_output_tokens == 5
    completed = [payload for status, payload in events if status == "completed"]
    assert len(completed) == 1
    reservation_id = completed[0]["reservation_id"]
    await runtime._settle_model_budget(
        1000,
        25,
        reservation_id=reservation_id,
        logical_call_id=completed[0]["logical_call_id"],
        input_tokens=20,
        actual_input_tokens=20,
        output_tokens=5,
        reservation_status="completed",
    )
    assert runtime._input_tokens == 20
    assert runtime._output_tokens == 5
    assert runtime._model_reservations == {}


@pytest.mark.anyio
async def test_v11_sdk_replay_cursor_rehydrates_and_repeated_replay_is_idempotent():
    logical_call_id = "logical-replay-history"
    original_request = f"{logical_call_id}:request-1"
    failed_request = f"{logical_call_id}:request-2"
    common = {
        "run_id": "run-replay-history",
        "attempt_id": "attempt-1",
        "phase": RuntimePhase.LEAD_PLANNING,
        "actor_type": RuntimeActorType.MODEL,
        "actor_name": "LeadAgent",
    }
    model_events = (
        RuntimeEvent(
            **common,
            sequence=1,
            event_type=RuntimeEventType.MODEL_STARTED,
            execution_id="execution-1",
            safe_payload={
                "status": "started",
                "logical_call_id": logical_call_id,
                "reservation_id": original_request,
                "reservation_status": "reserved",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 0,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=2,
            event_type=RuntimeEventType.MODEL_COMPLETED,
            execution_id="execution-1",
            safe_payload={
                "status": "completed",
                "input_tokens": 20,
                "output_tokens": 5,
                "logical_call_id": logical_call_id,
                "reservation_id": original_request,
                "reservation_status": "completed",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 0,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=3,
            event_type=RuntimeEventType.MODEL_STARTED,
            execution_id="execution-1",
            safe_payload={
                "status": "started",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "reserved",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 1,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=4,
            event_type=RuntimeEventType.MODEL_FAILED,
            execution_id="execution-1",
            safe_payload={
                "status": "failed",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "retrying",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 1,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=5,
            event_type=RuntimeEventType.MODEL_FAILED,
            execution_id="execution-recovery",
            safe_payload={
                "status": "failed",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "released",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 2,
                "request_index": 1,
            },
        ),
    )

    class ReplayDelegate(Model):
        async def get_response(self, **_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=3, output_tokens=4)
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    persisted: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        persisted.append((status, payload))

    runtime = V11Runtime(model=ReplayDelegate(), token_budget=975)
    runtime.bind_phase(
        PhaseInput(
            run_id="run-replay-history",
            attempt_id="attempt-2",
            phase=RuntimePhase.LEAD_PLANNING,
            resume_state=RuntimeResumeState(remaining_token_budget=975),
            token_budget=975,
            model_events=model_events,
        )
    )
    runtime._persist_model_event = persist_model_event
    settings = ModelSettings(max_tokens=100)
    first = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id=logical_call_id,
        execution_id="execution-2",
        attempt_number=1,
        actor="LeadAgent",
    )
    await first.get_response(
        input="replay request one",
        system_instructions="system",
        model_settings=settings,
    )
    await first.get_response(
        input="replay request two",
        system_instructions="system",
        model_settings=settings,
    )

    repeated = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id=logical_call_id,
        execution_id="execution-3",
        attempt_number=1,
        actor="LeadAgent",
    )
    await repeated.get_response(
        input="repeated replay request one",
        system_instructions="system",
        model_settings=settings,
    )

    started = [payload for status, payload in persisted if status == "started"]
    assert started[0]["reservation_id"] == (
        f"{logical_call_id}:replay-1:request-1"
    )
    assert started[1]["reservation_id"] == (
        f"{logical_call_id}:replay-1:request-2"
    )
    assert started[1]["reservation_id"] != failed_request
    assert started[2]["reservation_id"] == f"{logical_call_id}:replay-2:request-1"
    assert all(payload.get("request_index") is not None for payload in started)
    assert runtime._model_reservations == {}
    completed = [payload for status, payload in persisted if status == "completed"]
    assert runtime.remaining_token_budget == 975 - sum(
        int(payload.get("input_tokens", 0)) + int(payload.get("output_tokens", 0))
        for payload in completed
    )


@pytest.mark.anyio
async def test_v11_sdk_budget_reservation_is_released_when_preflight_rejects():
    class NoRequestModel(Model):
        async def get_response(self, *_args, **_kwargs):
            raise AssertionError("preflight must reject before provider request")

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    runtime = V11Runtime(model=NoRequestModel(), token_budget=100)
    proxy = _V11BudgetedModel(runtime.model, runtime)

    def reject_execution() -> None:
        raise V11RuntimeContractError("cancelled")

    runtime._check_execution = reject_execution
    with pytest.raises(V11RuntimeContractError, match="cancelled"):
        await proxy.get_response(
            system_instructions="inspect",
            input="one bounded request",
            model_settings=ModelSettings(max_tokens=80),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=SimpleNamespace(is_disabled=lambda: True),
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    assert runtime.remaining_token_budget == 100


@pytest.mark.anyio
async def test_v11_investigators_share_a_bounded_concurrent_gate():
    repository, record = _repository()
    runtime_run_id = "run-investigator-concurrency"
    tasks = [
        DiagnosisTask(
            id=f"task-concurrent-{index}",
            title="bounded investigation",
            description="inspect one isolated signal",
            task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
            agent_name="InvestigatorAgent",
            analysis_round=1,
            evidence_scope={"entity_ids": [record.event.service]},
            runtime_run_id=runtime_run_id,
        )
        for index in range(3)
    ]
    repository.save_plan(
        DiagnosisPlan(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            tasks=tasks,
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="run bounded investigators",
                task_ids=[task.id for task in tasks],
            ),
        )
    )
    active = 0
    peak = 0

    async def turn(**_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return {"summary": "completed", "findings": [], "candidates": []}
        finally:
            active -= 1

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=3,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert peak == 3
    assert active == 0


@pytest.mark.anyio
async def test_v11_all_investigator_failure_clears_prior_diagnostic_projection():
    repository, record = _repository()
    runtime_run_id = "run-investigator-terminal-projection"
    task = DiagnosisTask(
        id="task-terminal-projection",
        title="bounded investigation",
        description="inspect one isolated signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        evidence_scope={"entity_ids": [record.event.service]},
        runtime_run_id=runtime_run_id,
    )
    repository.save_plan(
        DiagnosisPlan(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="run investigator",
                task_ids=[task.id],
            ),
        )
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[
                RootCauseCandidate(
                    id="candidate-stale",
                    summary="stale projection",
                    rank=1,
                    confidence=0.5,
                )
            ],
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="stale decision",
                stop_reason="insufficient_evidence",
            ),
            diagnostic_status="inconclusive",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("all investigators unavailable")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.lead_decision is None
    assert review.diagnostic_status is None
    assert repository.get(record.id).multi_agent_run.diagnostic_status is None


@pytest.mark.anyio
async def test_v11_model_retry_is_one_classified_transport_attempt():
    repository, record = _repository()
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://provider.test/model")
            response = httpx.Response(429, request=request)
            raise openai.RateLimitError("limited", response=response, body={})
        return {
            "decision": {
                "action": "investigate",
                "summary": "retry succeeded",
                "task_ids": ["task-retry"],
            },
            "tasks": [
                {
                    "id": "task-retry",
                    "title": "Retry bounded task",
                    "description": "Inspect the committed signal.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-retry",
        remaining_tool_budget=8,
        remaining_token_budget=5000,
    )

    assert calls == 2
    executions = repository.list_executions(record.id)
    planning = [
        item
        for item in executions
        if item.step_kind == ExecutionStepKind.LEAD_PLANNING
    ]
    assert len(planning) == 2
    assert [item.attempt for item in planning] == [1, 2]
    assert planning[0].status == AgentExecutionStatus.FAILED
    assert planning[1].status == AgentExecutionStatus.COMPLETED
    assert planning[1].resume_from_execution_id == planning[0].id


@pytest.mark.anyio
async def test_v11_parse_failure_marks_latest_retry_attempt_invalid_output():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-parse-retry",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-parse-retry",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://provider.test/model")
            response = httpx.Response(429, request=request)
            raise openai.RateLimitError("limited", response=response, body={})
        return {"malformed": True}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        turn=turn,
        token_budget=1000,
    )
    runtime.runtime_run_id = "run-parse-retry"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert calls == 2
    assert [item.attempt for item in executions] == [1, 2]
    assert executions[0].status == AgentExecutionStatus.FAILED
    assert executions[1].status == AgentExecutionStatus.FAILED


@pytest.mark.anyio
async def test_v11_critic_success_has_one_durable_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-audited-critic",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-critic-audit",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "not enough committed evidence",
            "gap": "collect the missing signal",
        }
        for name in CausalCheckName
    ]

    async def turn(**_kwargs):
        return (
            {
                "summary": "critic audited",
                "assessments": [
                    {
                        "id": "assessment-audited-critic",
                        "candidate_id": candidate.id,
                        "verdict": CriticVerdict.INCONCLUSIVE.value,
                        "checks": checks,
                        "summary": "not enough committed evidence",
                    }
                ],
            },
            {"input_tokens": 3, "output_tokens": 5},
        )

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-critic-audit"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].status == AgentExecutionStatus.COMPLETED
    assert executions[0].runtime_run_id == "run-critic-audit"
    assert executions[0].input_tokens == 3
    assert executions[0].output_tokens == 5
    assert executions[0].deadline_at is not None


@pytest.mark.anyio
async def test_v11_critic_output_failure_updates_one_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-invalid-critic-output",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-invalid-critic-output",
            authority_mode="agent",
            candidates=[candidate],
        )
    )

    async def turn(**_kwargs):
        return {"summary": "missing assessment", "assessments": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-invalid-critic-output"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].status == AgentExecutionStatus.FAILED


@pytest.mark.anyio
async def test_v11_reconciliation_has_one_durable_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-reconciliation-audit",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    checks = [
        CausalCheck(
            name=name,
            status=CausalCheckStatus.UNKNOWN,
            summary="missing signal",
            gap="collect the signal",
        )
        for name in CausalCheckName
    ]
    assessment = CriticAssessment(
        id="assessment-reconciliation-audit",
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=checks,
        gap="collect the signal",
        supplemental_task_ids=["task-reconciliation-audit"],
        summary="needs one bounded signal",
        runtime_run_id="run-reconciliation-audit",
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-reconciliation-audit",
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(
        record.id,
        [
            DiagnosisTask(
                id="task-reconciliation-audit",
                title="Collect the requested signal",
                description="Close the named evidence gap.",
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name="InvestigatorAgent",
                tool_names=[],
                analysis_round=2,
                evidence_scope={"entity_ids": [record.event.service]},
                runtime_run_id="run-reconciliation-audit",
                critic_assessment_id=assessment.id,
            )
        ],
    )

    reconciled_checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "still missing signal",
            "gap": "collect the signal",
        }
        for name in CausalCheckName
    ]

    async def turn(**_kwargs):
        return (
            {
                "summary": "reconciled once",
                "assessments": [
                    {
                        "id": assessment.id,
                        "candidate_id": candidate.id,
                        "verdict": CriticVerdict.INCONCLUSIVE.value,
                        "checks": reconciled_checks,
                        "summary": "still inconclusive",
                    }
                ],
            },
            {"input_tokens": 2, "output_tokens": 4},
        )

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-reconciliation-audit"

    await runtime.critic_reconciliation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].analysis_round == 2
    assert executions[0].status == AgentExecutionStatus.COMPLETED
    assert executions[0].deadline_at is not None


@pytest.mark.anyio
async def test_v11_model_precharges_input_before_setting_output_cap():
    seen: list[dict] = []

    async def turn(**kwargs):
        seen.append(kwargs)
        return {"output": "bounded"}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        token_budget=1000,
    )

    await runtime._call_model(
        actor="LeadAgent",
        prompt="a long bounded prompt that must be charged before request",
        output_type=LeadPlanningOutput,
        context={"context": "also charged"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
    )

    assert seen[0]["remaining_token_budget"] < 1000
    assert seen[0]["max_output_tokens"] == seen[0]["remaining_token_budget"]


@pytest.mark.anyio
async def test_v11_model_deadline_preflight_blocks_request_before_start():
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        return {"output": "late"}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-deadline",
            attempt_id="attempt-deadline",
            phase="lead_planning",
            resume_state=RuntimeResumeState(),
            remaining_deadline_seconds=lambda: 0.0,
        )
    )

    with pytest.raises(V11RuntimeContractError, match="deadline"):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="must not start",
            output_type=LeadPlanningOutput,
            context={},
            tools=[],
            remaining_token_budget=None,
        )

    assert calls == 0


@pytest.mark.anyio
async def test_v11_zero_tool_budget_does_not_start_model():
    repository, record = _repository()
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        return {"output": "late"}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    with pytest.raises(V11RuntimeContractError, match="tool budget"):
        await runtime.plan_lead(
            repository=repository,
            investigation_id=record.id,
            event=record.event,
            runtime_run_id="run-zero-tool-budget",
            remaining_tool_budget=0,
            remaining_token_budget=None,
        )

    assert calls == 0


@pytest.mark.anyio
async def test_v11_all_investigator_failure_is_terminal_failed_without_diagnostic():
    repository, record = _repository()
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "plan one task",
                "task_ids": ["task-fail"],
            },
            "tasks": [
                {
                    "id": "task-fail",
                    "title": "Failing investigator",
                    "description": "The provider will fail before output.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        RuntimeError("investigator transport failed"),
    ]

    async def turn(**_kwargs):
        value = turns.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-investigator-failed",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert repository.get(record.id).status == InvestigationStatus.FAILED
    review = repository.get_coordination_review(record.id)
    assert review is None or review.diagnostic_status is None


@pytest.mark.anyio
async def test_v11_required_critic_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-failed-critic",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-critic-failed",
            authority_mode="agent",
            candidates=[candidate],
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("critic failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-critic-failed"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.critic_assessments == []
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_persistence_failure_is_terminal_failed_without_diagnostic():
    class FailOnceRepository(InMemoryInvestigationRepository):
        fail_next_review_save = False

        def save_coordination_review(self, review):
            if self.fail_next_review_save:
                self.fail_next_review_save = False
                raise RuntimeError("review persistence failed")
            return super().save_coordination_review(review)

    repository = FailOnceRepository()
    record = repository.save(InvestigationRecord(id="inv-persistence-failed", event=_event()))
    candidate = RootCauseCandidate(
        id="candidate-persistence-failed",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-persistence-failed",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    repository.fail_next_review_save = True

    async def turn(**_kwargs):
        return {"summary": "critic result", "assessments": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-persistence-failed"

    with pytest.raises(RuntimeError, match="review persistence failed"):
        await runtime.run_phase(
            "critic_review",
            repository=repository,
            investigation_id=record.id,
            event=record.event,
        )

    assert repository.get(record.id).status == InvestigationStatus.FAILED
    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.diagnostic_status is None
    assert review.run_status.value == "failed"


@pytest.mark.anyio
async def test_v11_required_lead_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-lead-failed",
            authority_mode="agent",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("lead failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-lead-failed"

    await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.lead_decision is None
    assert review.diagnostic_status is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_validator_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-validator-failed",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-validator-failed",
            authority_mode="agent",
            candidates=[candidate],
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="not enough evidence",
                stop_reason="insufficient_evidence",
            ),
            diagnostic_status="inconclusive",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("validator correction lead failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-validator-failed"

    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.diagnostic_status is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_critic_and_lead_authority_complete_without_semantic_validation():
    repository, record = _seed_repository()
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.PASS.value,
            "summary": "committed evidence supports this mechanical check",
            "evidence_ids": ["ev-metric"],
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "collect bounded evidence",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect the first signal",
                    "description": "Inspect the committed metric boundary.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "summary": "candidate from the investigator",
            "findings": [
                {
                    "finding_type": AgentFindingType.ROOT_CAUSE.value,
                    "summary": "bounded root-cause finding",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-metric"],
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "bounded candidate",
                    "rank": 1,
                    "confidence": 0.8,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "summary": "all seven checks are complete",
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": checks,
                    "summary": "accepted by the seven mechanical checks",
                }
            ],
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "accept the Critic-approved candidate",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=10000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    review = await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert len(review.critic_assessments) == 1
    assert len(review.critic_assessments[0].checks) == 7
    review = await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert review.lead_decision is not None
    assert review.lead_decision.candidate_ids == ["candidate-1"]
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert runtime.active_session_count == 0


@pytest.mark.anyio
async def test_v11_needs_evidence_has_one_round_two_batch_and_one_reconciliation():
    repository, record = _seed_repository()
    unknown_checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "named gap remains",
            "gap": "need one more signal",
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "collect evidence",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect signal",
                    "description": "Inspect the initial signal.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.GAP.value,
                    "summary": "one evidence gap",
                    "confidence": 0.4,
                    "gaps": ["need a trace boundary"],
                    "blocking": True,
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "candidate awaiting evidence",
                    "rank": 1,
                    "confidence": 0.5,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.NEEDS_EVIDENCE.value,
                    "checks": unknown_checks,
                    "gap": "need a trace boundary",
                    "supplemental_task_ids": ["task-round-2"],
                    "summary": "request one bounded evidence batch",
                }
            ],
            "tasks": [
                {
                    "id": "task-round-2",
                    "title": "Collect trace boundary",
                    "description": "Collect the requested trace boundary.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                    "information_gap": "need a trace boundary",
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.SIGNAL.value,
                    "summary": "trace boundary observed",
                    "confidence": 0.7,
                    "evidence_ids": ["ev-metric"],
                }
            ]
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": [
                        {
                            "name": name.value,
                            "status": CausalCheckStatus.PASS.value,
                            "summary": "committed evidence",
                            "evidence_ids": ["ev-metric"],
                        }
                        for name in CausalCheckName
                    ],
                    "summary": "reconciled",
                }
            ]
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "accept after reconciliation",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=10000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    first = await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert first.critic_assessments[0].verdict == CriticVerdict.NEEDS_EVIDENCE
    assert len(repository.list_tasks(record.id)) == 2
    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    reconciled = await runtime.critic_reconciliation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert reconciled is not None
    assert [item.id for item in reconciled.critic_assessments] == ["assessment-1"]
    assert reconciled.critic_assessments[0].verdict == CriticVerdict.ACCEPT
    assert repository.list_tasks(record.id)[-1].critic_assessment_id is not None
    assert len(turns) == 1
    await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )


@pytest.mark.anyio
async def test_v11_phase_executor_uses_runtime_phases_without_legacy_report_or_action():
    repository, record = _seed_repository()
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.PASS.value,
            "summary": "committed evidence",
            "evidence_ids": ["ev-metric"],
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "plan one investigator",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect signal",
                    "description": "Inspect the committed signal.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.ROOT_CAUSE.value,
                    "summary": "bounded finding",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-metric"],
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "bounded candidate",
                    "rank": 1,
                    "confidence": 0.8,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": checks,
                    "summary": "accepted",
                }
            ]
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "conclude accepted candidate",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    orchestrator = SimpleNamespace(
        repository=repository,
        v11_runtime=runtime,
        agents_runtime=None,
        max_total_tool_calls=8,
    )
    executor = DiagnosisPhaseExecutor(orchestrator)
    executor._is_durable_session = True
    for phase in V11_PHASE_ORDER:
        output = await executor.execute_phase(
            PhaseInput(
                run_id="run-v11",
                attempt_id="attempt-v11",
                phase=phase,
                resume_state=RuntimeResumeState(
                    remaining_tool_budget=8,
                    remaining_token_budget=10000,
                ),
                investigation_id=record.id,
                strategy="adaptive",
                run_reason=RuntimeRunReason.INITIAL,
                    execution_contract_version="v11",
                    execution_contract=_complete_contract(
                        runtime.tool_registry, model_name="fake"
                    ),
                    model_provider=ModelProvider.OPENAI,
                    model_name="fake",
                    tool_budget=8,
                token_budget=10000,
                timeout_seconds=60,
            )
        )
        assert output.business_mutation.investigation_id == record.id

    persisted = repository.get_coordination_review(record.id)
    assert persisted is not None
    assert persisted.lead_decision is not None
    assert repository.get(record.id).report is None
    assert repository.get(record.id).actions == []


def test_v11_official_provider_contract_and_client_ignore_ambient_endpoint(monkeypatch):
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.base_url = kwargs["base_url"]

        async def close(self):
            return None

    class FakeMultiProvider:
        def __init__(self, *, openai_client):
            self.openai_client = openai_client

        def get_model(self, _model_name):
            raise AssertionError("model construction is not part of this contract test")

        async def aclose(self):
            return None

    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(v11_runtime_module, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(v11_runtime_module, "MultiProvider", FakeMultiProvider)
    runtime = V11Runtime(model="fake", model_provider=ModelProvider.OPENAI)

    provider = v11_runtime_module._V11ModelProvider(runtime)

    assert captured["base_url"] == OFFICIAL_OPENAI_BASE_URL
    assert provider._client.base_url == OFFICIAL_OPENAI_BASE_URL
    container = AppContainer(AppSettings(storage=StorageSettings(url="memory://")))
    try:
        assert container._v11_model_identity(ModelProvider.OPENAI, "fake")[0] == endpoint_identity(
            OFFICIAL_OPENAI_BASE_URL
        )
    finally:
        container.close()


def test_v11_official_contract_survives_sqlite_reload_with_ambient_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    settings = AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'v11-official.db'}"),
        agents=AgentsSettings(enabled=True, model="gpt-test"),
    )
    first = AppContainer(settings)
    try:
        record = first.repository.save(
            InvestigationRecord(id="inv-official-reload", event=_event())
        )
        run = first.create_runtime_run(
            record.id,
            strategy="adaptive",
            run_reason="initial",
            execution_contract_version="v11",
        )
        expected_endpoint = endpoint_identity(OFFICIAL_OPENAI_BASE_URL)
        assert run.execution_contract["endpoint_id"] == expected_endpoint
        expected_digest = run.execution_contract["execution_contract_digest"]
    finally:
        first.close()

    second = AppContainer(settings)
    try:
        reloaded = second.runtime_store.get_run(run.id)
        assert reloaded.execution_contract["endpoint_id"] == expected_endpoint
        assert reloaded.execution_contract["execution_contract_digest"] == expected_digest
    finally:
        second.close()


@pytest.mark.anyio
async def test_v11_round_two_required_investigator_failure_terminalizes_before_reconciliation():
    repository, record = _repository()
    runtime_run_id = "run-round-two-failure"
    candidate = RootCauseCandidate(
        id="candidate-round-two",
        summary="candidate awaiting supplemental evidence",
        rank=1,
        confidence=0.5,
    )
    assessment = CriticAssessment(
        id="assessment-round-two",
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="named gap",
                gap="supplemental signal is unavailable",
            )
            for name in CausalCheckName
        ],
        gap="supplemental signal is unavailable",
        supplemental_task_ids=["task-round-two-failure"],
        summary="request one supplemental task",
        runtime_run_id=runtime_run_id,
    )
    task = DiagnosisTask(
        id="task-round-two-failure",
        title="Collect supplemental signal",
        description="The supplemental provider fails before returning output.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=2,
        evidence_scope={"entity_ids": [record.event.service]},
        runtime_run_id=runtime_run_id,
        critic_assessment_id=assessment.id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(record.id, [task])

    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("round two model transport failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert calls == 1
    assert repository.get(record.id).status == InvestigationStatus.FAILED
    assert repository.get(record.id).multi_agent_run is not None
    assert repository.get(record.id).multi_agent_run.completed_rounds == 0
    assert repository.get_coordination_review(record.id).candidates == []
    before = calls
    await runtime._dispatch_phase(
        phase="critic_reconciliation",
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id=runtime_run_id,
    )
    assert calls == before


@pytest.mark.anyio
async def test_v11_round_two_completed_partial_batch_is_not_blanket_failed():
    repository, record = _repository()
    runtime_run_id = "run-round-two-partial-success"
    task_ids = ["task-round-two-a", "task-round-two-b"]
    assessment = CriticAssessment(
        id="assessment-round-two-partial",
        candidate_id="candidate-round-two-partial",
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="named gap",
                gap="supplemental signal remains bounded",
            )
            for name in CausalCheckName
        ],
        gap="supplemental signal remains bounded",
        supplemental_task_ids=task_ids,
        summary="request two bounded signals",
        runtime_run_id=runtime_run_id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[
                RootCauseCandidate(
                    id=assessment.candidate_id,
                    summary="candidate",
                    rank=1,
                    confidence=0.4,
                )
            ],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(
        record.id,
        [
            DiagnosisTask(
                id=task_id,
                title=f"Collect signal {task_id[-1]}",
                description="A valid supplemental task with no new finding.",
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name="InvestigatorAgent",
                analysis_round=2,
                evidence_scope={"entity_ids": [record.event.service]},
                runtime_run_id=runtime_run_id,
                critic_assessment_id=assessment.id,
            )
            for task_id in task_ids
        ],
    )

    async def turn(**_kwargs):
        return {"findings": [], "candidates": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert repository.get(record.id).status != InvestigationStatus.FAILED
    assert repository.get(record.id).multi_agent_run.completed_rounds == 2
    assert all(
        item.status == AgentExecutionStatus.COMPLETED
        for item in repository.list_executions(record.id)
    )


@pytest.mark.anyio
async def test_v11_inconclusive_lead_clears_candidates_before_persist_and_reload():
    repository, record = _repository()
    runtime_run_id = "run-inconclusive-clears-candidates"
    candidate = RootCauseCandidate(
        id="candidate-inconclusive",
        summary="candidate without enough evidence",
        rank=1,
        confidence=0.4,
    )
    assessment = CriticAssessment(
        id="assessment-inconclusive",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="evidence remains insufficient",
                gap="the final decision does not rely on this candidate",
            )
            for name in CausalCheckName
        ],
        summary="candidate was assessed but not concluded",
        runtime_run_id=runtime_run_id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "the evidence gap remains",
                "stop_reason": "insufficient_evidence",
            }
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    review = await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    reloaded = repository.get_coordination_review(record.id)
    assert review is not None
    assert reloaded is not None
    assert review.candidates == []
    assert reloaded.candidates == []
    assert reloaded.critic_assessments == []
    assert reloaded.root_causes == []
    assert reloaded.lead_decision is not None
    assert reloaded.lead_decision.stop_reason == "insufficient_evidence"
    assert reloaded.diagnostic_status.value == "inconclusive"
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    validated = repository.get_coordination_review(record.id)
    assert validated is not None
    assert validated.candidates == []
    assert validated.diagnostic_status.value == "inconclusive"


@pytest.mark.anyio
async def test_v11_sdk_reservation_retry_reuses_one_durable_allocation_and_settles_once():
    calls = 0
    events: list[tuple[str, dict[str, object]]] = []

    class Delegate(Model):
        async def get_response(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=3, output_tokens=4)
            )

        async def stream_response(self, **_kwargs):
            if False:
                yield None

        async def close(self):
            return None

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, input_tokens, output_tokens, actor_name
        events.append((status, safe_payload or {}))

    runtime = V11Runtime(model=Delegate(), token_budget=100)
    runtime._persist_model_event = persist_model_event
    settings = ModelSettings(max_tokens=90)
    first = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-retry",
        execution_id="execution-1",
        attempt_number=1,
        actor="LeadAgent",
    )
    with pytest.raises(ClassifiedRetryableError):
        await first.get_response(
            input="one request",
            system_instructions="system",
            model_settings=settings,
        )

    assert runtime.remaining_token_budget == 10
    assert events[-1][0] == "failed"
    assert events[-1][1]["reservation_status"] == "retrying"

    second = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-retry",
        execution_id="execution-2",
        attempt_number=2,
        actor="LeadAgent",
    )
    await second.get_response(
        input="one request",
        system_instructions="system",
        model_settings=settings,
    )

    assert calls == 2
    assert [item[0] for item in events] == [
        "started",
        "failed",
        "started",
        "completed",
    ]
    assert len(
        {
            item[1]["reservation_id"]
            for item in events
            if item[1].get("reservation_id") is not None
        }
    ) == 1
    remaining_after_settle = runtime.remaining_token_budget
    await runtime._settle_model_budget(
        100,
        7,
        reservation_id="logical-call-retry:request-1",
        logical_call_id="logical-call-retry",
        execution_id="execution-2",
        input_tokens=3,
        output_tokens=4,
        reservation_status="completed",
    )
    assert runtime.remaining_token_budget == remaining_after_settle


@pytest.mark.anyio
async def test_v11_concurrent_model_reservations_cannot_oversell_token_ceiling():
    runtime = V11Runtime(model="fake", token_budget=100)

    async def reserve(index: int):
        return await runtime._reserve_model_budget(
            60,
            f"prompt-{index}",
            {"index": index},
            reservation_id=f"logical-{index}:request-1",
            logical_call_id=f"logical-{index}",
            execution_id=f"execution-{index}",
            actor="LeadAgent",
        )

    results = await asyncio.gather(*(reserve(index) for index in range(2)))

    assert sum(item[2] for item in results) == 100
    assert runtime.remaining_token_budget == 0
    for index, (_, input_estimate, reserved_total) in enumerate(results):
        await runtime._settle_model_budget(
            reserved_total,
            input_estimate,
            reservation_id=f"logical-{index}:request-1",
            logical_call_id=f"logical-{index}",
            execution_id=f"execution-{index}",
            input_tokens=input_estimate,
            reservation_status="completed",
        )
    assert runtime.remaining_token_budget == 90
