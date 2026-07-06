import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from backend.diagnosis.router import AgentRouter
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
)
from backend.domain.events import IncidentEvent
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class DiagnosisExecutionEngine:
    def __init__(
        self,
        repository: Any,
        tool_registry: ToolRegistry,
        router: AgentRouter | None = None,
    ) -> None:
        self.repository = repository
        self.tool_registry = tool_registry
        self.router = router or AgentRouter()

    def run(self, plan: DiagnosisPlan, event: IncidentEvent) -> DiagnosisPlan:
        tasks = [self.router.apply_route(task) for task in sorted(plan.tasks, key=_task_order)]
        plan = plan.model_copy(update={"tasks": tasks})
        self._save_plan(plan)

        status_by_id: dict[str, DiagnosisTaskStatus] = {}
        for index, task in enumerate(tasks):
            if reason := self._blocked_reason(task, status_by_id):
                task, execution = self._skip_task(task, reason)
            else:
                task, execution = self._run_task(plan.investigation_id, task, event)

            tasks[index] = task
            status_by_id[task.id] = task.status
            self._save_tasks(plan.investigation_id, tasks)
            self._save_executions(plan.investigation_id, [execution])

        completed_plan = plan.model_copy(update={"tasks": tasks})
        self._save_plan(completed_plan)
        return completed_plan

    def _run_task(
        self,
        investigation_id: str,
        task: DiagnosisTask,
        event: IncidentEvent,
    ) -> tuple[DiagnosisTask, AgentExecution]:
        started_at = datetime.now(UTC)
        started = perf_counter()
        task = task.model_copy(
            update={
                "status": DiagnosisTaskStatus.RUNNING,
                "started_at": started_at,
            }
        )

        calls = [
            self._invoke_tool(investigation_id, event, task, tool_name)
            for tool_name in task.tool_names
        ]
        failed_calls = [call for call in calls if call.status == ToolCallStatus.FAILED]
        execution_status = (
            AgentExecutionStatus.FAILED if failed_calls else AgentExecutionStatus.COMPLETED
        )
        task_status = (
            DiagnosisTaskStatus.FAILED if failed_calls else DiagnosisTaskStatus.COMPLETED
        )
        completed_at = datetime.now(UTC)
        error_message = "; ".join(
            call.error_message or f"{call.tool_name} failed" for call in failed_calls
        ) or None

        execution = AgentExecution(
            task_id=task.id,
            agent_name=task.agent_name,
            status=execution_status,
            tool_call_ids=[call.id for call in calls],
            evidence_ids=[
                evidence_id for call in calls for evidence_id in call.output_evidence_ids
            ],
            summary=f"{task.agent_name} {execution_status}",
            error_message=error_message,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return (
            task.model_copy(
                update={
                    "status": task_status,
                    "completed_at": completed_at,
                }
            ),
            execution,
        )

    def _invoke_tool(
        self,
        investigation_id: str,
        event: IncidentEvent,
        task: DiagnosisTask,
        tool_name: str,
    ) -> ToolCallRecord:
        tool_input = {"investigation_id": investigation_id}
        started_at = datetime.now(UTC)
        started = perf_counter()
        try:
            call = self.tool_registry.invoke(
                tool_name,
                event=event,
                task_id=task.id,
                agent_name=task.agent_name,
                input=tool_input,
            )
        except Exception as exc:
            logger.warning("tool call failed task_id=%s tool=%s reason=%s", task.id, tool_name, exc)
            call = ToolCallRecord(
                task_id=task.id,
                agent_name=task.agent_name,
                tool_name=tool_name,
                input=tool_input,
                status=ToolCallStatus.FAILED,
                error_message=str(exc),
                started_at=started_at,
                completed_at=datetime.now(UTC),
                duration_ms=int((perf_counter() - started) * 1000),
            )
        self._save_tool_calls(investigation_id, [call])
        return call

    def _skip_task(
        self,
        task: DiagnosisTask,
        reason: str,
    ) -> tuple[DiagnosisTask, AgentExecution]:
        now = datetime.now(UTC)
        task = task.model_copy(
            update={
                "status": DiagnosisTaskStatus.SKIPPED,
                "started_at": now,
                "completed_at": now,
            }
        )
        return (
            task,
            AgentExecution(
                task_id=task.id,
                agent_name=task.agent_name,
                status=AgentExecutionStatus.SKIPPED,
                summary=reason,
                error_message=reason,
                started_at=now,
                completed_at=now,
            ),
        )

    def _blocked_reason(
        self,
        task: DiagnosisTask,
        status_by_id: dict[str, DiagnosisTaskStatus],
    ) -> str | None:
        for dependency_id in task.depends_on:
            if status_by_id.get(dependency_id) != DiagnosisTaskStatus.COMPLETED:
                return f"dependency not completed: {dependency_id}"
        return None

    def _save_plan(self, plan: DiagnosisPlan) -> None:
        self._repo_call("save_plan", plan)

    def _save_tasks(self, investigation_id: str, tasks: list[DiagnosisTask]) -> None:
        self._repo_call("save_tasks", investigation_id, tasks)

    def _save_executions(
        self,
        investigation_id: str,
        executions: list[AgentExecution],
    ) -> None:
        self._repo_call("save_executions", investigation_id, executions)

    def _save_tool_calls(
        self,
        investigation_id: str,
        calls: list[ToolCallRecord],
    ) -> None:
        self._repo_call("save_tool_calls", investigation_id, calls)

    def _repo_call(self, method_name: str, *args: Any) -> None:
        method = getattr(self.repository, method_name, None)
        if callable(method):
            method(*args)


def _task_order(task: DiagnosisTask) -> tuple[int, bool]:
    return task.priority, bool(task.depends_on)


__all__ = ["DiagnosisExecutionEngine"]
