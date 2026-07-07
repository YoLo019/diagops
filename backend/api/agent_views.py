from typing import Any

from fastapi import APIRouter

from backend.api.investigations import _get_investigation_record
from backend.domain.agent_context import ContextFact
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.memory import MemoryItem
from backend.domain.tool_calls import ToolCallRecord
from backend.services.container import get_container

router = APIRouter(prefix="/investigations", tags=["agent-views"])


def _repository() -> Any:
    return get_container().repository


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
