from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.agent_views import _build_rca_graph_seed, _multi_agent_run_summary
from backend.domain.agent_plan import AgentExecution, AgentExecutionStatus
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
)
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


def test_v5_agentic_rca_endpoints_feed_workbench(
    client: TestClient,
    investigation_id: str,
):
    findings_response = client.get(f"/investigations/{investigation_id}/agent-findings")
    review_response = client.get(
        f"/investigations/{investigation_id}/coordination-review"
    )
    workbench_response = client.get(f"/investigations/{investigation_id}/rca-workbench")

    assert findings_response.status_code == 200
    assert review_response.status_code == 200
    assert workbench_response.status_code == 200

    findings = findings_response.json()
    review = review_response.json()
    workbench = workbench_response.json()

    assert findings
    assert review["candidates"]
    assert set(workbench) == {
        "investigation",
        "findings",
        "candidates",
        "evidence",
        "graph_seed",
        "coordination_review",
        "agent_executions",
        "multi_agent_run",
    }
    assert workbench["investigation"]["id"] == investigation_id
    assert workbench["findings"] == findings
    assert workbench["candidates"] == review["candidates"]
    assert workbench["coordination_review"] == review
    assert workbench["agent_executions"]
    assert workbench["agent_executions"] == client.get(
        f"/investigations/{investigation_id}/agent-executions"
    ).json()
    assert all(
        item["execution_layer"] == AgentExecutionLayer.CUSTOM
        for item in workbench["agent_executions"]
    )
    assert workbench["multi_agent_run"] is None
    assert workbench["evidence"]
    assert workbench["graph_seed"]["nodes"]
    assert workbench["graph_seed"]["edges"]
    node_ids = {node["id"] for node in workbench["graph_seed"]["nodes"]}
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in workbench["graph_seed"]["edges"]
    )


@pytest.mark.parametrize(
    "suffix",
    ["agent-findings", "coordination-review", "rca-workbench"],
)
def test_unknown_investigation_v5_agentic_rca_endpoints_return_404(
    client: TestClient,
    suffix: str,
):
    response = client.get(f"/investigations/inv-not-found/{suffix}")

    assert response.status_code == 404


def test_rca_graph_seed_drops_edges_with_missing_nodes():
    graph = _build_rca_graph_seed(
        evidence=[SimpleNamespace(id="ev-1", summary="Known evidence")],
        findings=[
            SimpleNamespace(
                id="finding-1",
                summary="Known finding",
                agent_name="LogAgent",
                evidence_ids=["ev-missing"],
            )
        ],
        candidates=[
            SimpleNamespace(
                id="candidate-1",
                summary="Known candidate",
                supporting_finding_ids=["finding-missing"],
                contradicting_finding_ids=[],
            )
        ],
    )

    node_ids = {node["id"] for node in graph["nodes"]}
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in graph["edges"]
    )


@pytest.mark.parametrize(
    "run_status", [MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL]
)
def test_workbench_prefers_persisted_sdk_review_over_failed_execution(
    client: TestClient,
    investigation_id: str,
    run_status: MultiAgentRunStatus,
):
    repository = get_container().repository
    review = repository.get_coordination_review(investigation_id).model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "run_status": run_status,
            "decision_status": (
                CoordinationDecisionStatus.AGREEMENT
                if run_status == MultiAgentRunStatus.COMPLETED
                else CoordinationDecisionStatus.FALLBACK
            ),
        }
    )
    repository.save_coordination_review(review)
    repository.save_executions(
        investigation_id,
        [
            AgentExecution(
                id="exec-sdk-failed",
                task_id="task-sdk",
                agent_name="CoordinatorAgent",
                status=AgentExecutionStatus.FAILED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                error_message="Bearer secret@example.com connection failed",
                started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
            )
        ],
    )

    workbench = client.get(f"/investigations/{investigation_id}/rca-workbench").json()

    assert workbench["coordination_review"] == review.model_dump(mode="json")
    assert workbench["candidates"] == workbench["coordination_review"]["candidates"]
    assert workbench["multi_agent_run"] == {
        "status": run_status,
        "failure_reason": None,
    }


@pytest.mark.parametrize(
    ("status", "error_message", "expected_reason"),
    [
        (
            AgentExecutionStatus.FAILED,
            "provider exploded Bearer token-secret secret@example.com connection=db",
            "Agents runtime failed",
        ),
        (
            AgentExecutionStatus.SKIPPED,
            "Agents runtime is not locally configured; Bearer token-secret secret@example.com",
            "Agents runtime not configured",
        ),
        (
            AgentExecutionStatus.SKIPPED,
            None,
            "Agents runtime not configured",
        ),
        (
            AgentExecutionStatus.FAILED,
            "ValidationError: invalid model output; Bearer token-secret secret@example.com",
            "Agents runtime returned invalid output",
        ),
    ],
)
def test_workbench_derives_failed_or_skipped_sdk_run_without_replacing_v5_review(
    client: TestClient,
    investigation_id: str,
    status: AgentExecutionStatus,
    error_message: str | None,
    expected_reason: str,
):
    repository = get_container().repository
    v5_review = repository.get_coordination_review(investigation_id)
    repository.save_executions(
        investigation_id,
        [
            AgentExecution(
                id="exec-sdk",
                task_id="task-sdk",
                agent_name="CoordinatorAgent",
                status=status,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                error_message=error_message,
                started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
            )
        ],
    )

    workbench = client.get(f"/investigations/{investigation_id}/rca-workbench").json()

    assert workbench["coordination_review"] == v5_review.model_dump(mode="json")
    assert workbench["candidates"] == v5_review.model_dump(mode="json")["candidates"]
    assert workbench["multi_agent_run"]["status"] == status
    assert workbench["multi_agent_run"]["failure_reason"] == expected_reason
    reason = expected_reason.lower()
    assert all(value not in reason for value in ("bearer", "secret", "@", "connection"))


def test_workbench_uses_latest_sdk_coordinator_attempt(
    client: TestClient,
    investigation_id: str,
):
    repository = get_container().repository
    repository.save_executions(
        investigation_id,
        [
            AgentExecution(
                id="exec-new",
                task_id="task-new",
                agent_name="CoordinatorAgent",
                status=AgentExecutionStatus.FAILED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                error_message="request timed out",
                started_at=datetime(2026, 7, 10, 10, tzinfo=UTC),
                completed_at=datetime(2026, 7, 10, 10, 1, tzinfo=UTC),
            ),
            AgentExecution(
                id="exec-old",
                task_id="task-old",
                agent_name="CoordinatorAgent",
                status=AgentExecutionStatus.SKIPPED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                error_message="not configured",
                started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
            ),
        ],
    )

    run = client.get(f"/investigations/{investigation_id}/rca-workbench").json()[
        "multi_agent_run"
    ]

    assert run == {
        "status": MultiAgentRunStatus.FAILED,
        "failure_reason": "Agents runtime timed out",
    }


def test_workbench_does_not_infer_completed_without_v7_review(
    client: TestClient,
    investigation_id: str,
):
    get_container().repository.save_executions(
        investigation_id,
        [
            AgentExecution(
                id="exec-sdk-completed",
                task_id="task-sdk",
                agent_name="CoordinatorAgent",
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
                completed_at=datetime(2026, 7, 10, 9, 1, tzinfo=UTC),
            )
        ],
    )

    workbench = client.get(f"/investigations/{investigation_id}/rca-workbench").json()

    assert workbench["multi_agent_run"] is None


def test_workbench_ignores_invalid_failed_sdk_review_and_uses_failed_execution(
    client: TestClient,
    investigation_id: str,
):
    repository = get_container().repository
    invalid_review = repository.get_coordination_review(investigation_id).model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "run_status": MultiAgentRunStatus.FAILED,
        }
    )
    repository.save_coordination_review(invalid_review)
    repository.save_executions(
        investigation_id,
        [
            AgentExecution(
                id="exec-sdk-failed",
                task_id="task-sdk",
                agent_name="CoordinatorAgent",
                status=AgentExecutionStatus.FAILED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                error_message="request timeout",
                started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
            )
        ],
    )

    run = client.get(f"/investigations/{investigation_id}/rca-workbench").json()[
        "multi_agent_run"
    ]

    assert run == {
        "status": MultiAgentRunStatus.FAILED,
        "failure_reason": "Agents runtime timed out",
    }


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 7, 10, 9, tzinfo=UTC), None],
)
def test_multi_agent_run_latest_tie_break_is_stable_by_execution_id(timestamp):
    failed = AgentExecution(
        id="exec-a",
        task_id="task-a",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        error_message="request timeout",
        started_at=timestamp,
    )
    skipped = AgentExecution(
        id="exec-z",
        task_id="task-z",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.SKIPPED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        started_at=timestamp,
    )

    expected = {
        "status": MultiAgentRunStatus.SKIPPED,
        "failure_reason": "Agents runtime not configured",
    }
    for executions in ([failed, skipped], [skipped, failed]):
        summary = _multi_agent_run_summary(None, executions)
        assert summary.model_dump(mode="json") == expected


def test_multi_agent_run_latest_treats_naive_datetimes_as_utc():
    aware_older = AgentExecution(
        id="exec-a",
        task_id="task-a",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        error_message="request timeout",
        started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
    )
    naive_newer = AgentExecution(
        id="exec-z",
        task_id="task-z",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.SKIPPED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        started_at=datetime(2026, 7, 10, 10),
    )

    for executions in ([aware_older, naive_newer], [naive_newer, aware_older]):
        summary = _multi_agent_run_summary(None, executions)
        assert summary.model_dump(mode="json") == {
            "status": MultiAgentRunStatus.SKIPPED,
            "failure_reason": "Agents runtime not configured",
        }
