from datetime import UTC, datetime

from backend.diagnosis.planner import DiagnosisTaskPlanner
from backend.domain.agent_plan import DiagnosisTaskType
from backend.domain.events import IncidentEvent, IncidentSource, Severity


def event(
    *,
    title: str = "Checkout errors",
    description: str = "Users see intermittent 500s.",
    signals: dict[str, str] | None = None,
) -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title=title,
        description=description,
        started_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        signals=signals or {},
    )


def task_types(plan):
    return [task.task_type for task in plan.tasks]


def test_basic_event_generates_core_tasks_with_stable_investigation_id():
    plan = DiagnosisTaskPlanner().plan(event(), investigation_id="inv-1")

    assert plan.investigation_id == "inv-1"
    assert task_types(plan) == [
        DiagnosisTaskType.LOG_INVESTIGATION,
        DiagnosisTaskType.METRIC_INVESTIGATION,
        DiagnosisTaskType.DEPLOYMENT_CHECK,
        DiagnosisTaskType.SERVICE_CONTEXT,
    ]
    assert [task.id for task in plan.tasks] == [
        "task-inv-1-log_investigation",
        "task-inv-1-metric_investigation",
        "task-inv-1-deployment_check",
        "task-inv-1-service_context",
    ]

    tasks = {task.task_type: task for task in plan.tasks}
    assert tasks[DiagnosisTaskType.LOG_INVESTIGATION].agent_name == "LogAgent"
    assert "read_logs" in tasks[DiagnosisTaskType.LOG_INVESTIGATION].tool_names
    assert tasks[DiagnosisTaskType.METRIC_INVESTIGATION].agent_name == "MetricAgent"
    assert "query_metrics" in tasks[DiagnosisTaskType.METRIC_INVESTIGATION].tool_names
    assert tasks[DiagnosisTaskType.DEPLOYMENT_CHECK].agent_name == "DeploymentAgent"
    assert "read_deployments" in tasks[DiagnosisTaskType.DEPLOYMENT_CHECK].tool_names
    assert tasks[DiagnosisTaskType.SERVICE_CONTEXT].agent_name == "ServiceCatalogAgent"
    assert "read_service_catalog" in tasks[DiagnosisTaskType.SERVICE_CONTEXT].tool_names


def test_timeout_or_dependency_adds_dependency_check():
    plan = DiagnosisTaskPlanner().plan(
        event(title="Payment dependency timeout", description="Checkout calls are slow."),
        investigation_id="inv-1",
    )

    dependency = next(
        task
        for task in plan.tasks
        if task.task_type == DiagnosisTaskType.DEPENDENCY_CHECK
    )
    assert dependency.agent_name == "DependencyAgent"
    assert "query_dependencies" in dependency.tool_names


def test_deploy_release_or_rollback_raises_deployment_priority():
    plan = DiagnosisTaskPlanner().plan(
        event(title="Rollback after bad release"),
        investigation_id="inv-1",
    )

    tasks = {task.task_type: task for task in plan.tasks}
    assert plan.tasks[0].task_type == DiagnosisTaskType.DEPLOYMENT_CHECK
    assert (
        tasks[DiagnosisTaskType.DEPLOYMENT_CHECK].priority
        < tasks[DiagnosisTaskType.LOG_INVESTIGATION].priority
    )


def test_qps_traffic_or_spike_raises_metric_priority():
    plan = DiagnosisTaskPlanner().plan(
        event(description="QPS traffic spike started at 09:00."),
        investigation_id="inv-1",
    )

    tasks = {task.task_type: task for task in plan.tasks}
    assert plan.tasks[0].task_type == DiagnosisTaskType.METRIC_INVESTIGATION
    assert (
        tasks[DiagnosisTaskType.METRIC_INVESTIGATION].priority
        < tasks[DiagnosisTaskType.LOG_INVESTIGATION].priority
    )


def test_signal_keys_and_values_participate_in_matching():
    plan = DiagnosisTaskPlanner().plan(
        event(
            title="Checkout errors",
            description="No obvious clue in the main text.",
            signals={"dependency": "payments", "load": "traffic spike"},
        ),
        investigation_id="inv-1",
    )

    assert DiagnosisTaskType.DEPENDENCY_CHECK in task_types(plan)
    assert plan.tasks[0].task_type == DiagnosisTaskType.METRIC_INVESTIGATION
