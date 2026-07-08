from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.agent_views import _build_rca_graph_seed
from backend.main import app
from backend.services.container import reset_container


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
    }
    assert workbench["investigation"]["id"] == investigation_id
    assert workbench["findings"] == findings
    assert workbench["candidates"] == review["candidates"]
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
