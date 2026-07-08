from dataclasses import dataclass

from backend.domain.agent_plan import DiagnosisTask, DiagnosisTaskType


@dataclass(frozen=True)
class AgentRoute:
    agent_name: str
    tool_names: tuple[str, ...] = ()


_FALLBACK_ROUTE = AgentRoute("ManualReviewAgent")

_ROUTES = {
    DiagnosisTaskType.LOG_INVESTIGATION.value: AgentRoute("LogAgent", ("read_logs",)),
    DiagnosisTaskType.METRIC_INVESTIGATION.value: AgentRoute(
        "MetricAgent",
        ("query_metrics",),
    ),
    DiagnosisTaskType.DEPLOYMENT_CHECK.value: AgentRoute(
        "DeploymentAgent",
        ("read_deployments",),
    ),
    DiagnosisTaskType.DEPENDENCY_CHECK.value: AgentRoute(
        "DependencyAgent",
        ("query_dependencies",),
    ),
    DiagnosisTaskType.SERVICE_CONTEXT.value: AgentRoute(
        "ServiceCatalogAgent",
        ("read_service_catalog",),
    ),
    DiagnosisTaskType.MEMORY_LOOKUP.value: AgentRoute("MemoryAgent", ("lookup_memory",)),
    DiagnosisTaskType.RCA_SYNTHESIS.value: AgentRoute("RcaAgent"),
    DiagnosisTaskType.LLM_REVIEW.value: AgentRoute("LlmAnalystAgent"),
}


class AgentRouter:
    def route(self, task_or_type: DiagnosisTask | DiagnosisTaskType | str) -> AgentRoute:
        return _ROUTES.get(self._task_type_value(task_or_type), _FALLBACK_ROUTE)

    def apply_route(self, task: DiagnosisTask) -> DiagnosisTask:
        route = self.route(task)
        return task.model_copy(
            update={
                "agent_name": route.agent_name,
                "tool_names": list(route.tool_names),
            },
        )

    def _task_type_value(self, task_or_type: DiagnosisTask | DiagnosisTaskType | str) -> str:
        if isinstance(task_or_type, DiagnosisTask):
            task_or_type = task_or_type.task_type
        if isinstance(task_or_type, DiagnosisTaskType):
            return task_or_type.value
        return str(task_or_type)


__all__ = ["AgentRoute", "AgentRouter"]
