import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from backend.diagnosis.router import AgentRouter
from backend.domain.agent_context import ContextFact, ContextFactType
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
)
from backend.domain.events import IncidentEvent
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.providers.results import ProviderResult, ProviderStatus
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

    def run(
        self,
        plan: DiagnosisPlan,
        event: IncidentEvent,
        provider_results: list[ProviderResult] | None = None,
    ) -> DiagnosisPlan:
        tasks = [self.router.apply_route(task) for task in sorted(plan.tasks, key=_task_order)]
        plan = plan.model_copy(update={"tasks": tasks})
        self._save_plan(plan)

        status_by_id: dict[str, DiagnosisTaskStatus] = {}
        task_ids = {task.id for task in tasks}
        pending_indexes = set(range(len(tasks)))
        while pending_indexes:
            progress = False
            for index in sorted(pending_indexes, key=lambda item: _task_order(tasks[item])):
                task = tasks[index]
                if reason := self._blocked_reason(task, status_by_id, task_ids):
                    task, execution = self._skip_task(task, reason)
                elif not self._dependencies_completed(task, status_by_id):
                    continue
                else:
                    task, execution = self._run_task(
                        plan.investigation_id,
                        task,
                        event,
                        provider_results,
                    )

                tasks[index] = task
                status_by_id[task.id] = task.status
                pending_indexes.remove(index)
                progress = True
                self._save_tasks(plan.investigation_id, tasks)
                self._save_executions(plan.investigation_id, [execution])
                self._save_context_fact(plan.investigation_id, execution)

            if progress:
                continue

            for index in sorted(pending_indexes, key=lambda item: _task_order(tasks[item])):
                task, execution = self._skip_task(
                    tasks[index],
                    "dependency cycle or unresolved dependency: "
                    + ", ".join(tasks[index].depends_on),
                )
                tasks[index] = task
                status_by_id[task.id] = task.status
                self._save_tasks(plan.investigation_id, tasks)
                self._save_executions(plan.investigation_id, [execution])
                self._save_context_fact(plan.investigation_id, execution)
            pending_indexes.clear()

        completed_plan = plan.model_copy(update={"tasks": tasks})
        self._save_plan(completed_plan)
        return completed_plan

    def _run_task(
        self,
        investigation_id: str,
        task: DiagnosisTask,
        event: IncidentEvent,
        provider_results: list[ProviderResult] | None,
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
            self._invoke_tool(investigation_id, event, task, tool_name, provider_results)
            for tool_name in task.tool_names
        ]
        failed_calls = [call for call in calls if call.status == ToolCallStatus.FAILED]
        skipped_calls = [call for call in calls if call.status == ToolCallStatus.SKIPPED]
        if failed_calls:
            execution_status = AgentExecutionStatus.FAILED
            task_status = DiagnosisTaskStatus.FAILED
            problem_calls = failed_calls + skipped_calls
        elif skipped_calls:
            execution_status = AgentExecutionStatus.SKIPPED
            task_status = DiagnosisTaskStatus.SKIPPED
            problem_calls = skipped_calls
        else:
            execution_status = AgentExecutionStatus.COMPLETED
            task_status = DiagnosisTaskStatus.COMPLETED
            problem_calls = []
        completed_at = datetime.now(UTC)
        error_message = "; ".join(
            call.error_message or f"{call.tool_name} {call.status}" for call in problem_calls
        ) or None

        execution = AgentExecution(
            task_id=task.id,
            agent_name=task.agent_name,
            status=execution_status,
            tool_call_ids=[call.id for call in calls],
            evidence_ids=[
                evidence_id for call in calls for evidence_id in call.output_evidence_ids
            ],
            summary=error_message
            if execution_status == AgentExecutionStatus.SKIPPED
            else f"{task.agent_name} {execution_status}",
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
        provider_results: list[ProviderResult] | None,
    ) -> ToolCallRecord:
        tool_input = {"investigation_id": investigation_id}
        started_at = datetime.now(UTC)
        started = perf_counter()
        try:
            spec = self.tool_registry.get(tool_name)
            if spec.provider is not None and provider_results is not None:
                call = _provider_tool_call(
                    task=task,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    provider_results=[
                        result
                        for result in provider_results
                        if result.provider == spec.provider
                    ],
                    started_at=started_at,
                    started=started,
                )
                self._save_tool_calls(investigation_id, [call])
                return call

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
        task_ids: set[str],
    ) -> str | None:
        for dependency_id in task.depends_on:
            if dependency_id not in task_ids:
                return f"dependency missing: {dependency_id}"
            if status_by_id.get(dependency_id) in {
                DiagnosisTaskStatus.FAILED,
                DiagnosisTaskStatus.SKIPPED,
            }:
                return f"dependency not completed: {dependency_id}"
        return None

    def _dependencies_completed(
        self,
        task: DiagnosisTask,
        status_by_id: dict[str, DiagnosisTaskStatus],
    ) -> bool:
        return all(
            status_by_id.get(dependency_id) == DiagnosisTaskStatus.COMPLETED
            for dependency_id in task.depends_on
        )

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

    def _save_context_fact(
        self,
        investigation_id: str,
        execution: AgentExecution,
    ) -> None:
        if not execution.evidence_ids:
            return
        self._repo_call(
            "save_context_facts",
            investigation_id,
            [
                ContextFact(
                    id=f"fact-{execution.task_id}",
                    source_agent=execution.agent_name,
                    fact_type=ContextFactType.OBSERVATION,
                    summary=f"{execution.agent_name} collected supporting evidence.",
                    confidence=0.8,
                    evidence_ids=execution.evidence_ids,
                )
            ],
        )

    def _repo_call(self, method_name: str, *args: Any) -> None:
        method = getattr(self.repository, method_name, None)
        if callable(method):
            method(*args)


def _task_order(task: DiagnosisTask) -> tuple[int, bool]:
    return task.priority, bool(task.depends_on)


def _provider_tool_call(
    *,
    task: DiagnosisTask,
    tool_name: str,
    tool_input: dict[str, Any],
    provider_results: list[ProviderResult],
    started_at: datetime,
    started: float,
) -> ToolCallRecord:
    if not provider_results:
        return ToolCallRecord(
            task_id=task.id,
            agent_name=task.agent_name,
            tool_name=tool_name,
            input=tool_input,
            status=ToolCallStatus.FAILED,
            error_message=f"no provider result for tool: {tool_name}",
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=int((perf_counter() - started) * 1000),
        )

    output_evidence_ids = [
        item.id for result in provider_results for item in result.evidence_items
    ]
    statuses = {result.status for result in provider_results}
    return ToolCallRecord(
        task_id=task.id,
        agent_name=task.agent_name,
        tool_name=tool_name,
        input=tool_input,
        status=(
            ToolCallStatus.FAILED
            if ProviderStatus.FAILED in statuses
            else ToolCallStatus.SUCCESS
            if output_evidence_ids
            or ProviderStatus.SUCCESS in statuses
            else ToolCallStatus.SKIPPED
        ),
        output_evidence_ids=output_evidence_ids,
        error_message=_provider_result_message(provider_results),
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_ms=int((perf_counter() - started) * 1000),
    )


def _provider_result_message(results: list[ProviderResult]) -> str | None:
    messages = [
        result.error_message or f"{result.provider} provider {result.status}"
        for result in results
        if result.status
        in {ProviderStatus.FAILED, ProviderStatus.PARTIAL, ProviderStatus.SKIPPED}
    ]
    return "; ".join(messages) or None


__all__ = ["DiagnosisExecutionEngine"]
