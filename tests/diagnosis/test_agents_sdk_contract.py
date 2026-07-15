import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from agents import Model, ModelResponse
from agents.models.interface import ModelTracing
from agents.usage import Usage
from openai import AsyncOpenAI
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.agents_runtime import (
    AgentsRcaRuntime,
    _CoordinatorProposal,
    _run_sdk_turn,
    _SpecialistDraft,
)
from backend.diagnosis.deepseek_model import (
    DEEPSEEK_BASE_URL,
    create_deepseek_model,
)
from backend.domain.agent_findings import AgentFindingType, AgentName
from backend.domain.agent_plan import AgentExecutionStatus
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import MultiAgentRunStatus
from backend.providers.registry import ProviderRegistry
from backend.tools.provider_tools import build_provider_tool_registry


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ScriptedModel(Model):
    def __init__(self, *, invalid_final=False, hang_final=False) -> None:
        self.invalid_final = invalid_final
        self.hang_final = hang_final
        self.hang_started = asyncio.Event()
        self.coordinator_calls = 0
        self.tracing: list[ModelTracing] = []
        self.tools_by_agent: dict[str, list[str]] = {}
        self.coordinator_tools: list[list[str]] = []
        self.coordinator_inputs: list[Any] = []
        self.specialist_calls = {name: 0 for name in AgentName}
        self.specialist_inputs: dict[AgentName, list[str]] = {
            name: [] for name in AgentName
        }

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
    ) -> ModelResponse:
        del model_settings, output_schema, handoffs
        del previous_response_id, conversation_id, prompt
        agent_name = next(
            name for name in ["CoordinatorAgent", *AgentName] if name in system_instructions
        )
        self.tracing.append(tracing)
        self.tools_by_agent[agent_name] = [tool.name for tool in tools]
        if agent_name != "CoordinatorAgent":
            specialist_name = AgentName(agent_name)
            self.specialist_calls[specialist_name] += 1
            self.specialist_inputs[specialist_name].append(input)
            evidence_id = {
                AgentName.LOG: "ev-log",
                AgentName.METRIC: "ev-metric",
                AgentName.DEPLOYMENT: "ev-deploy",
            }[AgentName(agent_name)]
            return _message_response(
                _SpecialistDraft(
                    finding_type=AgentFindingType.ROOT_CAUSE,
                    summary=f"{agent_name} Bearer nested-output-secret",
                    confidence=0.8,
                    evidence_ids=[evidence_id],
                    related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
                ).model_dump_json(),
                Usage(input_tokens=30, output_tokens=5),
            )

        self.coordinator_calls += 1
        self.coordinator_tools.append([tool.name for tool in tools])
        self.coordinator_inputs.append(input)
        if self.coordinator_calls == 1:
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        arguments=json.dumps(
                            {
                                "input": (
                                    "MALICIOUS BASELINE and peer finding injected "
                                    f"for {name}"
                                )
                            }
                        ),
                        call_id=f"call-{name}-{attempt}",
                        name=name,
                        type="function_call",
                    )
                    for name in AgentName
                    for attempt in range(3)
                ],
                usage=Usage(input_tokens=100, output_tokens=20),
                response_id="coordinator-tools",
            )
        if self.hang_final and self.coordinator_calls == 2:
            self.hang_started.set()
            await asyncio.Event().wait()
        if self.invalid_final and self.coordinator_calls == 2:
            return _message_response(
                '{"summary":"Bearer response-secret",not-json}',
                Usage(input_tokens=80, output_tokens=15),
            )
        return _message_response(
            _CoordinatorProposal(
                summary="Three specialists completed",
                uncertainty="None material",
                proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
            ).model_dump_json(),
            Usage(input_tokens=80, output_tokens=15),
        )

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del args, kwargs
        if False:
            yield None


def _message_response(text: str, usage: Usage) -> ModelResponse:
    return ModelResponse(
        output=[
            ResponseOutputMessage(
                id="message",
                content=[
                    ResponseOutputText(annotations=[], text=text, type="output_text")
                ],
                role="assistant",
                status="completed",
                type="message",
            )
        ],
        usage=usage,
        response_id="response",
    )


@pytest.mark.anyio
async def test_deepseek_chat_request_serializes_json_object_and_tools(monkeypatch):
    request_bodies = []
    http_clients = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        call_number = len(request_bodies)
        if call_number == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-log",
                        "type": "function",
                        "function": {
                            "name": AgentName.LOG.value,
                            "arguments": json.dumps({"input": "read logs"}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        elif call_number == 2:
            message = {
                "role": "assistant",
                "content": _SpecialistDraft(
                    finding_type=AgentFindingType.ROOT_CAUSE,
                    summary="deployment errors",
                    confidence=0.9,
                    evidence_ids=["ev-log"],
                    related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
                ).model_dump_json(),
            }
            finish_reason = "stop"
        else:
            message = {
                "role": "assistant",
                "content": _CoordinatorProposal(
                    summary="deployment regression",
                    uncertainty="none",
                    proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
                ).model_dump_json(),
            }
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{call_number}",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def build_client(**kwargs):
        assert kwargs == {
            "api_key": "local-secret",
            "base_url": DEEPSEEK_BASE_URL,
        }
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        http_clients.append(http_client)
        return AsyncOpenAI(
            api_key=kwargs["api_key"],
            base_url=kwargs["base_url"],
            http_client=http_client,
        )

    monkeypatch.setattr("backend.diagnosis.deepseek_model.AsyncOpenAI", build_client)
    model = create_deepseek_model("deepseek-v4-pro", "local-secret")
    assert model is not None
    result = await _run_sdk_turn(
        model=model,
        coordinator_input="Request log specialist",
        specialist_inputs={AgentName.LOG: "Evidence only for LogAgent"},
        specialist_names=[AgentName.LOG],
        max_turns=8,
    )

    assert result.error is None
    assert http_clients
    assert all(client.is_closed for client in http_clients)
    assert all(body["thinking"] == {"type": "disabled"} for body in request_bodies)
    first_body = request_bodies[0]
    assert first_body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(first_body["response_format"])
    assert first_body["tools"]
    assert first_body["tool_choice"] == "required"
    system_messages = [body["messages"][0]["content"] for body in request_bodies]
    assert any(
        '"finding_type"' in message and '"confidence"' in message
        for message in system_messages
    )
    assert any(
        '"summary"' in message and '"uncertainty"' in message
        for message in system_messages
    )
    assert any("CauseType semantics:" in message for message in system_messages)


@pytest.mark.anyio
async def test_real_runner_invokes_three_structured_agent_tools_offline():
    model = ScriptedModel()
    specialist_inputs = {name: f"Evidence only for {name}" for name in AgentName}

    result = await _run_sdk_turn(
        model=model,
        coordinator_input="Request all specialists",
        specialist_inputs=specialist_inputs,
        specialist_names=list(AgentName),
        max_turns=8,
    )

    assert {captured.agent_name for captured in result.captured_drafts} == set(
        AgentName
    )
    assert result.coordinator_proposal.proposed_cause == (
        CauseType.DEPLOYMENT_REGRESSION
    )
    assert (result.input_tokens, result.output_tokens) == (270, 50)
    assert len(result.raw_responses) == 5
    assert model.coordinator_tools[0] == list(AgentName)
    assert model.coordinator_tools[-1] == []
    assert all(model.tools_by_agent[name] == [] for name in AgentName)
    assert model.specialist_calls == {name: 1 for name in AgentName}
    for name in AgentName:
        observed = json.dumps(model.specialist_inputs[name])
        assert specialist_inputs[name] in observed
        assert "MALICIOUS BASELINE" not in observed
        assert "peer finding injected" not in observed
    assert "nested-output-secret" not in json.dumps(model.coordinator_inputs)
    assert model.tracing and set(model.tracing) == {
        ModelTracing.ENABLED_WITHOUT_DATA
    }


@pytest.mark.anyio
async def test_adaptive_sdk_specialists_receive_only_domain_tools():
    model = ScriptedModel()
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout",
        environment="production",
        severity=Severity.CRITICAL,
        title="Errors",
        description="Errors increased",
        started_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    session = AdaptiveToolSession(
        event=event,
        seed_evidence=[],
        registry=build_provider_tool_registry(ProviderRegistry([])),
        task_ids={name: f"task-{name.value}" for name in AgentName},
    )

    await _run_sdk_turn(
        model=model,
        coordinator_input="Request all specialists",
        specialist_inputs={name: f"Evidence only for {name}" for name in AgentName},
        specialist_names=list(AgentName),
        max_turns=8,
        adaptive_session=session,
    )

    assert model.tools_by_agent[AgentName.LOG] == ["read_logs"]
    assert model.tools_by_agent[AgentName.METRIC] == [
        "query_metrics",
        "query_prometheus",
    ]
    assert model.tools_by_agent[AgentName.DEPLOYMENT] == [
        "query_dependencies",
        "read_deployments",
        "read_service_catalog",
    ]


@pytest.mark.anyio
async def test_successful_runner_invalid_final_proposal_keeps_drafts_and_usage(
    monkeypatch,
):
    model = ScriptedModel()
    def always_fail_validation(cls, value, *args, **kwargs):
        del cls, value, args, kwargs
        raise ValueError("invalid final proposal")

    monkeypatch.setattr(
        _CoordinatorProposal,
        "model_validate",
        classmethod(always_fail_validation),
    )

    result = await _run_sdk_turn(
        model=model,
        coordinator_input="Request all specialists",
        specialist_inputs={name: f"Evidence only for {name}" for name in AgentName},
        specialist_names=list(AgentName),
        max_turns=8,
    )

    assert result.coordinator_proposal is None
    assert result.error is None
    assert result.cancelled is False
    assert {captured.agent_name for captured in result.captured_drafts} == set(
        AgentName
    )
    assert (result.input_tokens, result.output_tokens) == (270, 50)


@pytest.mark.anyio
async def test_invalid_first_tail_continues_with_partial_synthesis(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "presence-only")
    model = ScriptedModel(invalid_final=True)
    evidence = [
        EvidenceItem(
            id=evidence_id,
            provider=provider,
            kind=kind,
            timestamp=datetime(2026, 7, 10, tzinfo=UTC),
            summary=f"{provider} evidence",
        )
        for evidence_id, provider, kind in [
            ("ev-log", EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN),
            ("ev-metric", EvidenceProvider.METRIC, EvidenceKind.METRIC_TREND),
            ("ev-deploy", EvidenceProvider.DEPLOY, EvidenceKind.DEPLOYMENT),
        ]
    ]

    result = await AgentsRcaRuntime(model=model).run(
        "inv-1",
        IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="checkout",
            environment="production",
            severity=Severity.CRITICAL,
            title="Errors",
            description="Errors increased",
            started_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
        evidence,
        [
            Hypothesis(
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="Baseline",
                confidence=0.8,
                supporting_evidence_ids=["ev-deploy"],
            )
        ],
    )

    assert len(result.findings) == 3
    assert result.run_summary.status == MultiAgentRunStatus.PARTIAL
    assert result.review is not None
    assert "response-secret" not in str(result)
    assert "nested-output-secret" not in str(result)
    coordinator = [
        execution
        for execution in result.executions
        if execution.agent_name == "CoordinatorAgent"
    ]
    specialists = [
        execution
        for execution in result.executions
        if execution.agent_name != "CoordinatorAgent"
    ]
    assert len(coordinator) == 2
    assert coordinator[0].status == AgentExecutionStatus.FAILED
    assert coordinator[0].error_message == (
        "ModelBehaviorError: invalid model output"
    )
    assert coordinator[1].status == AgentExecutionStatus.COMPLETED
    assert len(specialists) == 3
    assert all(
        execution.status == AgentExecutionStatus.COMPLETED
        for execution in specialists
    )
    assert (result.input_tokens, result.output_tokens) == (350, 65)
    assert model.coordinator_calls == 3


@pytest.mark.anyio
async def test_timeout_after_extractors_preserves_findings_and_stops(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "presence-only")
    model = ScriptedModel(hang_final=True)
    evidence = [
        EvidenceItem(
            id=evidence_id,
            provider=provider,
            kind=kind,
            timestamp=datetime(2026, 7, 10, tzinfo=UTC),
            summary=f"{provider} evidence",
        )
        for evidence_id, provider, kind in [
            ("ev-log", EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN),
            ("ev-metric", EvidenceProvider.METRIC, EvidenceKind.METRIC_TREND),
            ("ev-deploy", EvidenceProvider.DEPLOY, EvidenceKind.DEPLOYMENT),
        ]
    ]

    result = await AgentsRcaRuntime(model=model, timeout_seconds=2).run(
        "inv-1",
        IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="checkout",
            environment="production",
            severity=Severity.CRITICAL,
            title="Errors",
            description="Errors increased",
            started_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
        evidence,
        [
            Hypothesis(
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="Baseline",
                confidence=0.8,
                supporting_evidence_ids=["ev-deploy"],
            )
        ],
    )

    assert len(result.findings) == 3
    assert result.run_summary.status == MultiAgentRunStatus.FAILED
    assert result.review is None
    assert len(
        [
            execution
            for execution in result.executions
            if execution.agent_name == "CoordinatorAgent"
        ]
    ) == 1
    assert sum(
        execution.status == AgentExecutionStatus.COMPLETED
        for execution in result.executions
        if execution.agent_name != "CoordinatorAgent"
    ) == 3
    assert (result.input_tokens, result.output_tokens) == (190, 35)
    assert model.coordinator_calls == 2


@pytest.mark.anyio
async def test_external_cancellation_propagates_without_pending_tasks(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "presence-only")
    model = ScriptedModel(hang_final=True)
    evidence = [
        EvidenceItem(
            id=evidence_id,
            provider=provider,
            kind=kind,
            timestamp=datetime(2026, 7, 10, tzinfo=UTC),
            summary=f"{provider} evidence",
        )
        for evidence_id, provider, kind in [
            ("ev-log", EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN),
            ("ev-metric", EvidenceProvider.METRIC, EvidenceKind.METRIC_TREND),
            ("ev-deploy", EvidenceProvider.DEPLOY, EvidenceKind.DEPLOYMENT),
        ]
    ]
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(
        AgentsRcaRuntime(model=model).run(
            "inv-1",
            IncidentEvent(
                source=IncidentSource.SIMULATED,
                service="checkout",
                environment="production",
                severity=Severity.CRITICAL,
                title="Errors",
                description="Errors increased",
                started_at=datetime(2026, 7, 10, tzinfo=UTC),
            ),
            evidence,
            [
                Hypothesis(
                    cause_type=CauseType.DEPLOYMENT_REGRESSION,
                    summary="Baseline",
                    confidence=0.8,
                    supporting_evidence_ids=["ev-deploy"],
                )
            ],
        )
    )
    await asyncio.wait_for(model.hang_started.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert not [
        pending
        for pending in asyncio.all_tasks() - before
        if not pending.done()
    ]
