import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.agent_context import (
    ContextFact,
    ContextFactType,
)
from backend.domain.agent_findings import (
    CoordinationReview,
    RootCauseAttribution,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
    DiagnosisTaskType,
)
from backend.domain.evidence import EvidenceProvider
from backend.domain.hypotheses import CauseType
from backend.domain.memory import MemoryItem, MemoryType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    ExecutionStepKind,
    FailureCategory,
    ModelProvider,
    ResultValidationCategory,
)
from backend.domain.tool_calls import ToolSpec


def test_v82_cause_types_are_available():
    assert {item.value for item in CauseType} >= {
        "resource_saturation",
        "network_fault",
        "configuration_error",
        "process_or_container_failure",
        "infrastructure_fault",
    }


def test_root_cause_attribution_requires_grounded_non_empty_values():
    attribution = RootCauseAttribution(
        root_cause_component="checkout-service",
        root_cause_occurred_at=datetime(2026, 7, 15, 8, tzinfo=UTC),
        root_cause_reason="CPU saturation aligned with latency increase.",
        supporting_evidence_ids=["ev-metric-1"],
    )

    assert attribution.supporting_evidence_ids == ["ev-metric-1"]
    assert CoordinationReview(investigation_id="inv-1").root_causes == []

    for changes in (
        {"root_cause_component": ""},
        {"root_cause_reason": ""},
        {"root_cause_occurred_at": datetime(2026, 7, 15, 8)},
        {"supporting_evidence_ids": []},
        {"supporting_evidence_ids": [""]},
    ):
        values = attribution.model_dump()
        values.update(changes)
        with pytest.raises(ValidationError):
            RootCauseAttribution.model_validate(values)


def test_coordination_review_preserves_root_cause_order_and_sorts_candidates():
    """F17：root_causes 排序所有权在 attribution 构建层，domain validator 不再
    按 occurred_at 重排；candidates 仍按 rank 排序。"""

    def attribution(hour: int) -> RootCauseAttribution:
        return RootCauseAttribution(
            root_cause_component=f"service-{hour}",
            root_cause_occurred_at=datetime(2026, 7, 15, hour, tzinfo=UTC),
            root_cause_reason="bounded evidence",
            supporting_evidence_ids=[f"ev-{hour}"],
        )

    def candidate(rank: int) -> RootCauseCandidate:
        return RootCauseCandidate(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary=f"candidate {rank}",
            rank=rank,
            confidence=0.8,
        )

    review = CoordinationReview(
        investigation_id="inv-1",
        candidates=[candidate(2), candidate(1)],
        root_causes=[attribution(9), attribution(8)],
    )

    assert [item.rank for item in review.candidates] == [1, 2]
    assert [item.root_cause_component for item in review.root_causes] == [
        "service-9",
        "service-8",
    ]


def test_agent_models_defaults_are_readable_and_empty_lists_are_fresh():
    task = DiagnosisTask(
        title="Read logs",
        description="Collect error patterns.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="LogAgent",
    )
    plan = DiagnosisPlan(investigation_id="inv-1")
    execution = AgentExecution(task_id=task.id, agent_name="LogAgent")
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
    assert memory.tags == []
    assert task.execution_layer == AgentExecutionLayer.CUSTOM
    assert task.analysis_round is None
    assert execution.execution_layer == AgentExecutionLayer.CUSTOM
    assert execution.analysis_round is None
    assert execution.step_kind is None
    assert execution.attempt == 1
    assert execution.failure_category == FailureCategory.NONE
    assert execution.result_validation_category is None
    assert execution.model_provider is None
    assert execution.model_name is None


def test_agent_execution_rejects_attempt_below_one():
    with pytest.raises(ValidationError):
        AgentExecution(task_id="task-1", agent_name="LogAgent", attempt=0)


def test_agent_execution_round_trips_result_validation_category():
    execution = AgentExecution(
        task_id="task-validation",
        agent_name="CoordinatorAgent",
        status="failed",
        execution_layer="openai_agents_sdk",
        step_kind="result_validation",
        runtime_run_id="run-validation",
        failure_category="invalid_output",
        result_validation_category="review_contract",
    )

    reloaded = AgentExecution.model_validate_json(execution.model_dump_json())

    assert (
        reloaded.result_validation_category
        == ResultValidationCategory.REVIEW_CONTRACT
    )


def test_agent_execution_uses_pydantic_28_protected_namespaces():
    assert AgentExecution.model_config["protected_namespaces"] == (
        "model_validate",
        "model_dump",
    )


@pytest.mark.parametrize("model", [DiagnosisTask, AgentExecution])
def test_task_and_execution_reject_invalid_analysis_round(model):
    common = {"agent_name": "LogAgent", "analysis_round": 3}
    if model is DiagnosisTask:
        common.update(
            title="Read logs",
            description="Collect error patterns.",
            task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        )
    else:
        common["task_id"] = "task-1"

    with pytest.raises(ValidationError):
        model(**common)


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
    execution = AgentExecution(
        task_id=task.id,
        agent_name="LogAgent",
        step_kind=ExecutionStepKind.SPECIALIST_COLLECTION,
        failure_category=FailureCategory.TIMEOUT,
        model_provider=ModelProvider.DEEPSEEK,
        model_name="deepseek-test",
    )

    dumped = DiagnosisPlan(investigation_id="inv-1", tasks=[task]).model_dump(mode="json")
    execution_dumped = execution.model_dump(mode="json")
    json.dumps(dumped)
    json.dumps(execution_dumped)

    assert dumped["tasks"][0]["status"] == "pending"
    assert execution_dumped["step_kind"] == "specialist_collection"
    assert execution_dumped["failure_category"] == "timeout"
    assert execution_dumped["model_provider"] == "deepseek"
