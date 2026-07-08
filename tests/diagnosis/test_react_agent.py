from datetime import UTC, datetime

from backend.diagnosis.react_agent import (
    LlmToolCall,
    ReActInvestigationAgent,
    ReActLlmResponse,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.react_trace import ReActTraceStatus
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.tools.registry import ToolRegistry


class FakeLlm:
    def __init__(self, responses: list[ReActLlmResponse]) -> None:
        self.responses = list(responses)
        self.messages = []
        self.tools = []

    def generate(self, messages, tools):
        self.messages.append(messages)
        self.tools.append(tools)
        return self.responses.pop(0)


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="500 spike",
        description="checkout has 500s",
        started_at=datetime.now(UTC),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs.", read_only=True),
        lambda **kwargs: ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
            input=kwargs["input"],
            status=ToolCallStatus.SUCCESS,
            output_evidence_ids=["ev-log"],
        ),
    )
    registry.register(
        ToolSpec(name="remediate", description="Mutate prod.", read_only=False),
        lambda **kwargs: ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
            input=kwargs["input"],
            status=ToolCallStatus.SUCCESS,
        ),
    )
    return registry


def test_react_agent_executes_tool_then_records_final_answer():
    llm = FakeLlm(
        [
            ReActLlmResponse(
                content="Need logs.",
                tool_call=LlmToolCall(name="read_logs", arguments={"extra": "ignored"}),
            ),
            ReActLlmResponse(content="Final answer cites ev-log."),
        ]
    )

    trace = ReActInvestigationAgent(llm=llm, tool_registry=_registry()).run(
        investigation_id="inv-1",
        event=_event(),
        existing_evidence_ids=["ev-existing"],
    )

    assert trace.status == ReActTraceStatus.COMPLETED
    assert trace.final_answer == "Final answer cites ev-log."
    assert trace.steps[0].tool_name == "read_logs"
    assert trace.steps[0].tool_input == {"investigation_id": "inv-1"}
    assert trace.steps[0].output_evidence_ids == ["ev-log"]
    assert llm.tools[0] == [
        {
            "name": "read_logs",
            "description": "Read logs.",
            "parameters": {
                "type": "object",
                "properties": {"investigation_id": {"type": "string"}},
                "required": ["investigation_id"],
            },
            "read_only": True,
        }
    ]
    assert "ev-existing" in str(llm.messages[0])
    assert "ev-log" in str(llm.messages[1])


def test_react_agent_fails_closed_for_unknown_tool():
    llm = FakeLlm(
        [ReActLlmResponse(tool_call=LlmToolCall(name="bash", arguments={}))]
    )

    trace = ReActInvestigationAgent(llm=llm, tool_registry=_registry()).run(
        investigation_id="inv-1",
        event=_event(),
        existing_evidence_ids=[],
    )

    assert trace.status == ReActTraceStatus.FAILED
    assert trace.steps[0].status == "failed"
    assert "unsupported" in (trace.steps[0].error_message or "")


def test_react_agent_stops_at_max_steps():
    llm = FakeLlm(
        [
            ReActLlmResponse(tool_call=LlmToolCall(name="read_logs", arguments={})),
            ReActLlmResponse(tool_call=LlmToolCall(name="read_logs", arguments={})),
        ]
    )

    trace = ReActInvestigationAgent(llm=llm, tool_registry=_registry(), max_steps=2).run(
        investigation_id="inv-1",
        event=_event(),
        existing_evidence_ids=[],
    )

    assert trace.status == ReActTraceStatus.MAX_STEPS
    assert len(trace.steps) == 2
