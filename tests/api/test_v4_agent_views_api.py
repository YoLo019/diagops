import pytest
from fastapi.testclient import TestClient

from backend.domain.agent_plan import DiagnosisPlan, DiagnosisTask, DiagnosisTaskType
from backend.main import app
from backend.services.container import get_container, reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def investigation_id(client: TestClient) -> str:
    created = client.post("/events/simulated/deployment_regression")
    assert created.status_code == 200
    return created.json()["id"]


def test_agent_view_endpoints_return_data(
    client: TestClient,
    investigation_id: str,
):
    plan_response = client.get(f"/investigations/{investigation_id}/plan")
    tasks_response = client.get(f"/investigations/{investigation_id}/tasks")
    executions_response = client.get(
        f"/investigations/{investigation_id}/agent-executions"
    )
    context_response = client.get(f"/investigations/{investigation_id}/context")
    tool_calls_response = client.get(f"/investigations/{investigation_id}/tool-calls")
    graph_response = client.get(f"/investigations/{investigation_id}/task-graph")

    assert plan_response.status_code == 200
    assert tasks_response.status_code == 200
    assert executions_response.status_code == 200
    assert context_response.status_code == 200
    assert tool_calls_response.status_code == 200
    assert graph_response.status_code == 200

    plan = plan_response.json()
    tasks = tasks_response.json()
    graph = graph_response.json()
    assert plan["investigation_id"] == investigation_id
    assert plan["tasks"]
    assert tasks
    assert executions_response.json()
    context = context_response.json()
    assert context
    assert all(item["evidence_ids"] for item in context)
    assert tool_calls_response.json()
    assert graph["nodes"]
    assert {node["id"] for node in graph["nodes"]} == {task["id"] for task in tasks}


def test_memory_endpoint_returns_feedback_memory(
    client: TestClient,
    investigation_id: str,
):
    feedback_response = client.post(
        f"/investigations/{investigation_id}/feedback",
        json={
            "root_cause_correct": True,
            "action_useful": True,
            "verification_result": "passed",
            "note": "rollback fixed the incident",
        },
    )
    memory_response = client.get(f"/investigations/{investigation_id}/memory")

    assert feedback_response.status_code == 200
    assert memory_response.status_code == 200
    memory = memory_response.json()
    assert [item["id"] for item in memory] == [feedback_response.json()["id"]]


def test_task_graph_nodes_cover_tasks_and_edges_follow_dependencies(
    client: TestClient,
    investigation_id: str,
):
    root = DiagnosisTask(
        id="task-root",
        title="Read logs",
        description="Read incident logs.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="LogAgent",
    )
    dependent = DiagnosisTask(
        id="task-dependent",
        title="Synthesize RCA",
        description="Use log findings to synthesize RCA.",
        task_type=DiagnosisTaskType.RCA_SYNTHESIS,
        agent_name="RcaAgent",
        depends_on=[root.id],
    )
    get_container().repository.save_plan(
        DiagnosisPlan(investigation_id=investigation_id, tasks=[root, dependent])
    )

    response = client.get(f"/investigations/{investigation_id}/task-graph")

    assert response.status_code == 200
    graph = response.json()
    assert {node["id"] for node in graph["nodes"]} == {root.id, dependent.id}
    assert {
        (
            node["id"],
            node["label"],
            node["title"],
            node["type"],
            node["status"],
            node["agent_name"],
        )
        for node in graph["nodes"]
    } == {
        (
            root.id,
            root.title,
            root.title,
            root.task_type,
            root.status,
            root.agent_name,
        ),
        (
            dependent.id,
            dependent.title,
            dependent.title,
            dependent.task_type,
            dependent.status,
            dependent.agent_name,
        ),
    }
    assert graph["edges"] == [{"source": root.id, "target": dependent.id}]


@pytest.mark.parametrize(
    "suffix",
    [
        "plan",
        "tasks",
        "agent-executions",
        "context",
        "tool-calls",
        "memory",
        "task-graph",
    ],
)
def test_unknown_investigation_agent_view_endpoints_return_404(
    client: TestClient,
    suffix: str,
):
    response = client.get(f"/investigations/inv-not-found/{suffix}")

    assert response.status_code == 404
