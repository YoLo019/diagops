from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)


def test_react_trace_step_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        ReActTraceStep(
            step_number=1,
            tool_name="bash",
            tool_input={"investigation_id": "inv-1"},
            status=ReActTraceStepStatus.FAILED,
            started_at=datetime.now(UTC),
        )


def test_react_trace_step_requires_positive_step_number():
    with pytest.raises(ValidationError):
        ReActTraceStep(step_number=0, status=ReActTraceStepStatus.THINKING)


@pytest.mark.parametrize(
    "tool_input",
    [
        {"bad": float("nan")},
        {"nested": [float("inf")]},
    ],
)
def test_react_trace_step_rejects_non_finite_tool_input_floats(tool_input):
    with pytest.raises(ValidationError):
        ReActTraceStep(step_number=1, tool_input=tool_input)


def test_react_trace_orders_steps_and_defaults_to_json_safe_fields():
    later = ReActTraceStep(step_number=2, status=ReActTraceStepStatus.COMPLETED)
    first = ReActTraceStep(
        step_number=1,
        assistant_text="Need deployment evidence.",
        tool_name="read_deployments",
        tool_input={"investigation_id": "inv-1"},
        output_evidence_ids=["ev-deploy"],
        status=ReActTraceStepStatus.OBSERVED,
    )

    trace = ReActTrace(
        investigation_id="inv-1",
        status=ReActTraceStatus.COMPLETED,
        final_answer="Deployment evidence supports the top hypothesis.",
        steps=[later, first],
    )

    assert [step.step_number for step in trace.steps] == [1, 2]
    assert trace.model_dump(mode="json")["steps"][0]["tool_name"] == "read_deployments"
