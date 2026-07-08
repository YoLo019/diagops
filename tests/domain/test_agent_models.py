import json

import pytest
from pydantic import ValidationError

from backend.domain.agent_context import (
    ContextFact,
    ContextFactType,
    SharedInvestigationContext,
)
from backend.domain.agent_plan import (
    AgentExecution,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
    DiagnosisTaskType,
)
from backend.domain.evidence import EvidenceProvider
from backend.domain.memory import MemoryItem, MemoryType
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec


def test_agent_models_defaults_are_readable_and_empty_lists_are_fresh():
    task = DiagnosisTask(
        title="Read logs",
        description="Collect error patterns.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="LogAgent",
    )
    plan = DiagnosisPlan(investigation_id="inv-1")
    execution = AgentExecution(task_id=task.id, agent_name="LogAgent")
    context = SharedInvestigationContext(investigation_id="inv-1")
    memory = MemoryItem(
        service="checkout-service",
        environment="prod",
        memory_type=MemoryType.INVESTIGATION_SUMMARY,
        summary="Previous checkout incident was a deployment regression.",
    )

    assert task.id.startswith("task-")
    assert plan.id.startswith("plan-")
    assert execution.id.startswith("exec-")
    assert memory.id.startswith("mem-")
    assert task.tool_names == []
    assert plan.tasks == []
    assert context.facts == []
    assert memory.tags == []


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_context_fact_rejects_invalid_confidence(confidence):
    with pytest.raises(ValidationError):
        ContextFact(
            source_agent="RcaAgent",
            fact_type=ContextFactType.HYPOTHESIS,
            summary="Deployment regression is likely.",
            confidence=confidence,
            evidence_ids=["ev-1"],
        )


def test_missing_evidence_fact_can_omit_evidence_ids():
    fact = ContextFact(
        source_agent="RcaAgent",
        fact_type=ContextFactType.MISSING_EVIDENCE,
        summary="Need metrics from the incident window.",
        confidence=0.4,
    )

    assert fact.evidence_ids == []


def test_regular_context_fact_requires_evidence_ids():
    with pytest.raises(ValidationError, match="evidence_ids required"):
        ContextFact(
            source_agent="RcaAgent",
            fact_type=ContextFactType.CONCLUSION,
            summary="Deployment regression is likely.",
            confidence=0.9,
        )


def test_tool_spec_defaults_to_read_only():
    spec = ToolSpec(
        name="read_logs",
        description="Read application logs.",
        provider=EvidenceProvider.LOG,
    )

    assert spec.read_only is True


def test_task_status_is_explicit_enum():
    task = DiagnosisTask(
        title="Synthesize RCA",
        description="Summarize the most likely root cause.",
        task_type="rca_synthesis",
        agent_name="RcaAgent",
        status="running",
    )

    assert task.status == DiagnosisTaskStatus.RUNNING


def test_agent_models_dump_json_payloads():
    task = DiagnosisTask(
        title="Read logs",
        description="Collect error patterns.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="LogAgent",
        tool_names=["read_logs"],
    )
    tool_call = ToolCallRecord(
        task_id=task.id,
        agent_name="LogAgent",
        tool_name="read_logs",
        input={"service": "checkout-service", "window_minutes": 30},
        status=ToolCallStatus.SUCCESS,
        output_evidence_ids=["ev-1"],
        duration_ms=12,
    )
    fact = ContextFact(
        source_agent="LogAgent",
        fact_type=ContextFactType.OBSERVATION,
        summary="500 errors increased after deployment.",
        confidence=0.86,
        evidence_ids=["ev-1"],
    )
    context = SharedInvestigationContext(
        investigation_id="inv-1",
        facts=[fact],
        evidence_ids=["ev-1"],
        tool_calls=[tool_call],
    )

    dumped = DiagnosisPlan(investigation_id="inv-1", tasks=[task]).model_dump(mode="json")
    json.dumps(dumped)
    json.dumps(context.model_dump(mode="json"))

    assert dumped["tasks"][0]["status"] == "pending"
