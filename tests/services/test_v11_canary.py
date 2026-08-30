"""V11 local workflow capability observation tests."""

import pytest

from backend.diagnosis.v11_runtime import CriticCompactOutput
from backend.runtime.faults import DeterministicFaultInjector
from backend.services.v11_canary import run_v11_local_workflow_canary


@pytest.mark.anyio
async def test_local_workflow_canary_uses_compact_runtime_boundary() -> None:
    """真实 SDK/Store/Tool 边界必须完成完整 V11 workflow。"""
    assert await run_v11_local_workflow_canary() is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault_point",
    ["tool_after_commit_before_checkpoint", "model_after_send", "parallel_session_failure"],
)
async def test_local_workflow_canary_fails_closed_on_runtime_fault(
    fault_point: str,
) -> None:
    injector = DeterministicFaultInjector(
        {fault_point: 1}
    )
    assert await run_v11_local_workflow_canary(fault_injector=injector) is False


def test_compact_critic_schema_declares_bounded_supplemental_work() -> None:
    schema = CriticCompactOutput.model_json_schema()
    assessment = schema["$defs"]["CriticCompactAssessmentDraft"]
    assert "needs_evidence" in assessment["properties"]["verdict"]["enum"]
    assert "supplemental_task_ids" in assessment["properties"]
    assert "CriticCompactTaskDraft" in schema["$defs"]
