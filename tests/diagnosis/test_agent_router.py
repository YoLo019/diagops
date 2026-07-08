import pytest

from backend.diagnosis.router import AgentRoute, AgentRouter
from backend.domain.agent_plan import DiagnosisTask, DiagnosisTaskType


@pytest.mark.parametrize(
    ("task_type", "route"),
    [
        (
            DiagnosisTaskType.LOG_INVESTIGATION,
            AgentRoute("LogAgent", ("read_logs",)),
        ),
        (
            DiagnosisTaskType.METRIC_INVESTIGATION,
            AgentRoute("MetricAgent", ("query_metrics",)),
        ),
        (
            DiagnosisTaskType.DEPLOYMENT_CHECK,
            AgentRoute("DeploymentAgent", ("read_deployments",)),
        ),
        (
            DiagnosisTaskType.DEPENDENCY_CHECK,
            AgentRoute("DependencyAgent", ("query_dependencies",)),
        ),
        (
            DiagnosisTaskType.SERVICE_CONTEXT,
            AgentRoute("ServiceCatalogAgent", ("read_service_catalog",)),
        ),
        (
            DiagnosisTaskType.MEMORY_LOOKUP,
            AgentRoute("MemoryAgent", ("lookup_memory",)),
        ),
        (
            DiagnosisTaskType.RCA_SYNTHESIS,
            AgentRoute("RcaAgent"),
        ),
        (
            DiagnosisTaskType.LLM_REVIEW,
            AgentRoute("LlmAnalystAgent"),
        ),
    ],
)
def test_known_task_types_route_to_agent_and_tools(task_type, route):
    assert AgentRouter().route(task_type) == route
    assert AgentRouter().route(task_type.value) == route


def test_unknown_task_type_routes_to_manual_review_without_tools():
    route = AgentRouter().route("needs_human")

    assert route == AgentRoute("ManualReviewAgent")


def test_apply_route_returns_routed_copy_without_mutating_original():
    task = DiagnosisTask(
        title="Read logs",
        description="Collect error patterns.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="OldAgent",
        tool_names=["old_tool"],
    )

    routed = AgentRouter().apply_route(task)

    assert routed is not task
    assert routed.agent_name == "LogAgent"
    assert routed.tool_names == ["read_logs"]
    assert task.agent_name == "OldAgent"
    assert task.tool_names == ["old_tool"]
