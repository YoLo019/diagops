from backend.domain.agent_plan import DiagnosisPlan, DiagnosisTask, DiagnosisTaskType
from backend.domain.events import IncidentEvent

_BASE_PRIORITY = 100
_RAISED_PRIORITY = 50

_TASK_ORDER = {
    DiagnosisTaskType.LOG_INVESTIGATION: 0,
    DiagnosisTaskType.METRIC_INVESTIGATION: 1,
    DiagnosisTaskType.DEPLOYMENT_CHECK: 2,
    DiagnosisTaskType.DEPENDENCY_CHECK: 3,
    DiagnosisTaskType.SERVICE_CONTEXT: 4,
}

_BASE_TASKS = (
    (
        DiagnosisTaskType.LOG_INVESTIGATION,
        "Investigate logs",
        "Read incident-window logs for error patterns.",
        "LogAgent",
        ("read_logs",),
    ),
    (
        DiagnosisTaskType.METRIC_INVESTIGATION,
        "Investigate metrics",
        "Query incident-window service metrics.",
        "MetricAgent",
        ("query_metrics",),
    ),
    (
        DiagnosisTaskType.DEPLOYMENT_CHECK,
        "Check deployments",
        "Read deployment changes near the incident window.",
        "DeploymentAgent",
        ("read_deployments",),
    ),
    (
        DiagnosisTaskType.SERVICE_CONTEXT,
        "Read service context",
        "Read service ownership and dependency context.",
        "ServiceCatalogAgent",
        ("read_service_catalog",),
    ),
)

_DEPENDENCY_TASK = (
    DiagnosisTaskType.DEPENDENCY_CHECK,
    "Check dependencies",
    "Query dependency health and related service context.",
    "DependencyAgent",
    ("query_dependencies",),
)


class DiagnosisTaskPlanner:
    dependency_keywords = ("timeout", "dependency")
    deployment_keywords = ("deploy", "release", "rollback")
    metric_keywords = ("qps", "traffic", "spike")

    def plan(self, event: IncidentEvent, investigation_id: str = "") -> DiagnosisPlan:
        text = self._event_text(event)
        specs = list(_BASE_TASKS)
        if self._contains_any(text, self.dependency_keywords):
            specs.append(_DEPENDENCY_TASK)

        priority_by_type: dict[DiagnosisTaskType, int] = {}
        if self._contains_any(text, self.deployment_keywords):
            priority_by_type[DiagnosisTaskType.DEPLOYMENT_CHECK] = _RAISED_PRIORITY
        if self._contains_any(text, self.metric_keywords):
            priority_by_type[DiagnosisTaskType.METRIC_INVESTIGATION] = _RAISED_PRIORITY

        tasks = [
            DiagnosisTask(
                id=self._task_id(task_type, investigation_id),
                title=title,
                description=description,
                task_type=task_type,
                agent_name=agent_name,
                tool_names=list(tool_names),
                priority=priority_by_type.get(task_type, _BASE_PRIORITY),
            )
            for task_type, title, description, agent_name, tool_names in specs
        ]
        tasks.sort(key=lambda task: (task.priority, _TASK_ORDER[task.task_type]))
        return DiagnosisPlan(investigation_id=investigation_id, tasks=tasks)

    def _event_text(self, event: IncidentEvent) -> str:
        signals = " ".join(f"{key} {value}" for key, value in event.signals.items())
        return f"{event.title} {event.description} {signals}".casefold()

    def _contains_any(self, text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _task_id(self, task_type: DiagnosisTaskType, investigation_id: str) -> str:
        if investigation_id:
            return f"task-{investigation_id}-{task_type.value}"
        return f"task-{task_type.value}"


__all__ = ["DiagnosisTaskPlanner"]
