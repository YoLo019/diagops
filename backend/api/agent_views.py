from datetime import UTC
from typing import Any

from fastapi import APIRouter

from backend.api.config import build_agent_config
from backend.api.investigations import _get_investigation_record
from backend.diagnosis.agents_runtime import (
    stabilization_categories_from_executions,
)
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.memory import MemoryItem
from backend.domain.multi_agent import (
    AdaptiveRunStatus,
    AdaptiveStopReason,
    AgentExecutionLayer,
    FailureCategory,
    InvestigationStrategy,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.react_trace import ReActTrace
from backend.domain.tool_calls import ToolCallRecord
from backend.safety.redaction import redact_model
from backend.services.container import get_container

router = APIRouter(prefix="/investigations", tags=["agent-views"])


def _repository() -> Any:
    return get_container().repository


def _list_agent_findings(investigation_id: str) -> list[AgentFinding]:
    return [
        redact_model(item)
        for item in _repository().list_agent_findings(investigation_id)
    ]


def _get_coordination_review(investigation_id: str) -> CoordinationReview | None:
    review = _repository().get_coordination_review(investigation_id)
    return None if review is None else redact_model(review)


def _get_react_trace(investigation_id: str) -> ReActTrace | None:
    trace = _repository().get_react_trace(investigation_id)
    return None if trace is None else redact_model(trace)


_SAFE_FAILURE_LABELS = {
    FailureCategory.NOT_CONFIGURED: "Agents runtime not configured",
    FailureCategory.AUTHENTICATION: "Agents runtime authentication failed",
    FailureCategory.RATE_LIMIT: "Agents runtime rate limited",
    FailureCategory.QUOTA: "Agents runtime quota exceeded",
    FailureCategory.TIMEOUT: "Agents runtime timed out",
    FailureCategory.CANCELLED: "Agents runtime cancelled",
    FailureCategory.TRANSPORT: "Agents runtime transport failed",
    FailureCategory.INVALID_OUTPUT: "Agents runtime returned invalid output",
    FailureCategory.INVALID_REFERENCE: "Agents runtime returned invalid references",
    FailureCategory.MISSING_SPECIALIST: "Agents runtime missed a required specialist",
    FailureCategory.UNSAFE_OUTPUT: "Agents runtime returned unsafe output",
    FailureCategory.PERSISTENCE: "Agents review persistence failed",
}


def _safe_failure_label(category: FailureCategory) -> str:
    return _SAFE_FAILURE_LABELS.get(category, "Agents runtime failed")


def _execution_timestamp(execution: AgentExecution) -> float:
    timestamp = execution.completed_at or execution.started_at
    return (
        float("-inf")
        if timestamp is None
        else timestamp.replace(tzinfo=timestamp.tzinfo or UTC).timestamp()
    )


def _multi_agent_run_summary(
    review: CoordinationReview | None,
    executions: list[AgentExecution],
    *,
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED,
    tool_calls: list[ToolCallRecord] | None = None,
    max_tool_calls_per_specialist: int = 3,
    max_total_tool_calls: int = 8,
) -> MultiAgentRunSummary | None:
    summary: MultiAgentRunSummary | None = None
    if (
        review is not None
        and review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
        and review.run_status
        in {MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL}
    ):
        summary = MultiAgentRunSummary(
            status=review.run_status,
            model_provider=review.model_provider,
            model_name=review.model_name,
            primary_stabilization_category=(
                review.primary_stabilization_category
            ),
            secondary_stabilization_categories=(
                review.secondary_stabilization_categories
            ),
        )
    else:
        attempts = [
            execution
            for execution in executions
            if execution.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
            and execution.agent_name == "CoordinatorAgent"
        ]
        if attempts:
            latest = max(
                attempts,
                key=lambda execution: (
                    _execution_timestamp(execution),
                    execution.id,
                ),
            )
            if latest.status.value in {
                MultiAgentRunStatus.FAILED.value,
                MultiAgentRunStatus.SKIPPED.value,
            }:
                categories = stabilization_categories_from_executions(executions)
                summary = MultiAgentRunSummary(
                    status=MultiAgentRunStatus(latest.status.value),
                    failure_reason=_safe_failure_label(latest.failure_category),
                    model_provider=latest.model_provider,
                    model_name=latest.model_name,
                    primary_stabilization_category=(
                        categories[0] if categories else None
                    ),
                    secondary_stabilization_categories=categories[1:],
                )

    if strategy != InvestigationStrategy.ADAPTIVE:
        return summary
    calls = tool_calls or []
    if summary is None:
        return MultiAgentRunSummary(
            status=MultiAgentRunStatus.SKIPPED,
            strategy=strategy,
            adaptive_status=AdaptiveRunStatus.SKIPPED,
            tool_call_count=len(calls),
            max_tool_calls_per_specialist=max_tool_calls_per_specialist,
            max_total_tool_calls=max_total_tool_calls,
        )
    adaptive_status = (
        AdaptiveRunStatus.COMPLETED
        if summary.status == MultiAgentRunStatus.COMPLETED
        else (
            AdaptiveRunStatus.SKIPPED
            if summary.status == MultiAgentRunStatus.SKIPPED
            else AdaptiveRunStatus.DEGRADED
        )
    )
    return summary.model_copy(
        update={
            "strategy": strategy,
            "adaptive_status": adaptive_status,
            "adaptive_stop_reason": _adaptive_stop_reason(summary, calls),
            "tool_call_count": len(calls),
            "max_tool_calls_per_specialist": max_tool_calls_per_specialist,
            "max_total_tool_calls": max_total_tool_calls,
        }
    )


def _adaptive_stop_reason(
    summary: MultiAgentRunSummary,
    calls: list[ToolCallRecord],
) -> AdaptiveStopReason | None:
    messages = " ".join(call.error_message or "" for call in calls).lower()
    for marker, reason in (
        ("budget exhausted", AdaptiveStopReason.BUDGET_EXHAUSTED),
        ("duplicate query", AdaptiveStopReason.DUPLICATE_QUERY),
        ("no new evidence", AdaptiveStopReason.NO_NEW_EVIDENCE),
    ):
        if marker in messages:
            return reason
    if summary.primary_stabilization_category is not None and (
        summary.primary_stabilization_category.value == "cancelled_or_timeout"
    ):
        return AdaptiveStopReason.TIMEOUT
    if summary.status == MultiAgentRunStatus.FAILED:
        return AdaptiveStopReason.FAILED
    return None


@router.get("/{investigation_id}/plan", response_model=DiagnosisPlan | None)
def get_investigation_plan(investigation_id: str) -> DiagnosisPlan | None:
    _get_investigation_record(investigation_id)
    plan = _repository().get_plan(investigation_id)
    return None if plan is None else redact_model(plan)


@router.get("/{investigation_id}/tasks", response_model=list[DiagnosisTask])
def list_investigation_tasks(investigation_id: str) -> list[DiagnosisTask]:
    _get_investigation_record(investigation_id)
    return [
        redact_model(item) for item in _repository().list_tasks(investigation_id)
    ]


@router.get("/{investigation_id}/agent-executions", response_model=list[AgentExecution])
def list_investigation_agent_executions(
    investigation_id: str,
) -> list[AgentExecution]:
    _get_investigation_record(investigation_id)
    return [
        redact_model(item)
        for item in _repository().list_executions(investigation_id)
    ]


@router.get("/{investigation_id}/context", response_model=list[ContextFact])
def list_investigation_context(investigation_id: str) -> list[ContextFact]:
    _get_investigation_record(investigation_id)
    return [
        redact_model(item)
        for item in _repository().list_context_facts(investigation_id)
    ]


@router.get("/{investigation_id}/tool-calls", response_model=list[ToolCallRecord])
def list_investigation_tool_calls(investigation_id: str) -> list[ToolCallRecord]:
    _get_investigation_record(investigation_id)
    return [
        redact_model(item)
        for item in _repository().list_tool_calls(investigation_id)
    ]


@router.get("/{investigation_id}/memory", response_model=list[MemoryItem])
def list_investigation_memory(investigation_id: str) -> list[MemoryItem]:
    record = _get_investigation_record(investigation_id)
    return [
        redact_model(item)
        for item in _repository().list_memory(record.event.service, record.event.environment)
    ]


@router.get("/{investigation_id}/agent-findings", response_model=list[AgentFinding])
def list_investigation_agent_findings(
    investigation_id: str,
) -> list[AgentFinding]:
    _get_investigation_record(investigation_id)
    return _list_agent_findings(investigation_id)


@router.get(
    "/{investigation_id}/coordination-review",
    response_model=CoordinationReview | None,
)
def get_investigation_coordination_review(
    investigation_id: str,
) -> CoordinationReview | None:
    _get_investigation_record(investigation_id)
    return _get_coordination_review(investigation_id)


@router.get("/{investigation_id}/react-trace", response_model=ReActTrace | None)
def get_investigation_react_trace(investigation_id: str) -> ReActTrace | None:
    _get_investigation_record(investigation_id)
    return _get_react_trace(investigation_id)


@router.get("/{investigation_id}/rca-workbench")
def get_investigation_rca_workbench(investigation_id: str) -> dict[str, Any]:
    record = _get_investigation_record(investigation_id)
    findings = _list_agent_findings(investigation_id)
    review = _get_coordination_review(investigation_id)
    executions = [
        redact_model(item)
        for item in _repository().list_executions(investigation_id)
    ]
    tasks = _repository().list_tasks(investigation_id)
    tool_calls = [
        redact_model(item)
        for item in _repository().list_tool_calls(investigation_id)
    ]
    custom_task_ids = {
        task.id
        for task in tasks
        if task.execution_layer == AgentExecutionLayer.CUSTOM
    }
    adaptive_tool_calls = [
        call for call in tool_calls if call.task_id not in custom_task_ids
    ]
    workbench_tool_calls = (
        adaptive_tool_calls
        if record.strategy == InvestigationStrategy.ADAPTIVE
        else tool_calls
    )
    agent_config = build_agent_config()
    candidates = [] if review is None else review.candidates
    return {
        "investigation": record,
        "findings": findings,
        "candidates": candidates,
        "evidence": record.evidence,
        "graph_seed": _build_rca_graph_seed(record.evidence, findings, candidates),
        "coordination_review": review,
        "agent_executions": executions,
        "tool_calls": workbench_tool_calls,
        "multi_agent_run": record.multi_agent_run
        or _multi_agent_run_summary(
            review,
            executions,
            strategy=record.strategy,
            tool_calls=adaptive_tool_calls,
            max_tool_calls_per_specialist=(
                agent_config.max_tool_calls_per_specialist
            ),
            max_total_tool_calls=agent_config.max_total_tool_calls,
        ),
        "agent_config": agent_config,
    }


def _build_rca_graph_seed(evidence, findings, candidates) -> dict[str, list[dict[str, str]]]:
    nodes: dict[str, dict[str, str]] = {}
    edges: list[dict[str, str]] = []

    for item in evidence:
        nodes[item.id] = {"id": item.id, "label": item.summary, "type": "evidence"}
    for finding in findings:
        agent_id = str(finding.agent_name)
        nodes[agent_id] = {"id": agent_id, "label": agent_id, "type": "agent"}
        nodes[finding.id] = {
            "id": finding.id,
            "label": finding.summary,
            "type": "finding",
        }
        edges.append({"source": agent_id, "target": finding.id, "relation": "produced"})
        edges.extend(
            {"source": finding.id, "target": evidence_id, "relation": "cites"}
            for evidence_id in finding.evidence_ids
        )
    for candidate in candidates:
        nodes[candidate.id] = {
            "id": candidate.id,
            "label": candidate.summary,
            "type": "candidate",
        }
        edges.extend(
            {"source": finding_id, "target": candidate.id, "relation": "supports"}
            for finding_id in candidate.supporting_finding_ids
        )
        edges.extend(
            {"source": finding_id, "target": candidate.id, "relation": "contradicts"}
            for finding_id in candidate.contradicting_finding_ids
        )

    node_ids = set(nodes)
    return {
        "nodes": list(nodes.values()),
        "edges": [
            edge
            for edge in edges
            if edge["source"] in node_ids and edge["target"] in node_ids
        ],
    }


@router.get("/{investigation_id}/task-graph")
def get_investigation_task_graph(investigation_id: str):
    _get_investigation_record(investigation_id)
    tasks = _repository().list_tasks(investigation_id)
    return {
        "nodes": [
            {
                "id": task.id,
                "label": task.title,
                "title": task.title,
                "type": task.task_type,
                "status": task.status,
                "agent_name": task.agent_name,
            }
            for task in tasks
        ],
        "edges": [
            {"source": dependency_id, "target": task.id}
            for task in tasks
            for dependency_id in task.depends_on
        ],
    }
