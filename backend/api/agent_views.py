from typing import Any

from fastapi import APIRouter

from backend.api.investigations import _get_investigation_record
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.memory import MemoryItem
from backend.domain.react_trace import ReActTrace
from backend.domain.tool_calls import ToolCallRecord
from backend.services.container import get_container

router = APIRouter(prefix="/investigations", tags=["agent-views"])


def _repository() -> Any:
    return get_container().repository


def _list_agent_findings(investigation_id: str) -> list[AgentFinding]:
    list_findings = getattr(_repository(), "list_agent_findings", None)
    if not callable(list_findings):
        return []
    return list_findings(investigation_id)


def _get_coordination_review(investigation_id: str) -> CoordinationReview | None:
    get_review = getattr(_repository(), "get_coordination_review", None)
    if not callable(get_review):
        return None
    return get_review(investigation_id)


def _get_react_trace(investigation_id: str) -> ReActTrace | None:
    get_trace = getattr(_repository(), "get_react_trace", None)
    if not callable(get_trace):
        return None
    return get_trace(investigation_id)


@router.get("/{investigation_id}/plan", response_model=DiagnosisPlan | None)
def get_investigation_plan(investigation_id: str) -> DiagnosisPlan | None:
    _get_investigation_record(investigation_id)
    return _repository().get_plan(investigation_id)


@router.get("/{investigation_id}/tasks", response_model=list[DiagnosisTask])
def list_investigation_tasks(investigation_id: str) -> list[DiagnosisTask]:
    _get_investigation_record(investigation_id)
    return _repository().list_tasks(investigation_id)


@router.get("/{investigation_id}/agent-executions", response_model=list[AgentExecution])
def list_investigation_agent_executions(
    investigation_id: str,
) -> list[AgentExecution]:
    _get_investigation_record(investigation_id)
    return _repository().list_executions(investigation_id)


@router.get("/{investigation_id}/context", response_model=list[ContextFact])
def list_investigation_context(investigation_id: str) -> list[ContextFact]:
    _get_investigation_record(investigation_id)
    list_context_facts = getattr(_repository(), "list_context_facts", None)
    if not callable(list_context_facts):
        return []
    return list_context_facts(investigation_id)


@router.get("/{investigation_id}/tool-calls", response_model=list[ToolCallRecord])
def list_investigation_tool_calls(investigation_id: str) -> list[ToolCallRecord]:
    _get_investigation_record(investigation_id)
    return _repository().list_tool_calls(investigation_id)


@router.get("/{investigation_id}/memory", response_model=list[MemoryItem])
def list_investigation_memory(investigation_id: str) -> list[MemoryItem]:
    record = _get_investigation_record(investigation_id)
    list_memory = getattr(_repository(), "list_memory", None)
    if not callable(list_memory):
        return []
    return list_memory(record.event.service, record.event.environment)


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
    candidates = [] if review is None else review.candidates
    return {
        "investigation": record,
        "findings": findings,
        "candidates": candidates,
        "evidence": record.evidence,
        "graph_seed": _build_rca_graph_seed(record.evidence, findings, candidates),
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
