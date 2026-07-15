from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.api.config as config_api
from backend.api.agent_views import _build_rca_graph_seed, _multi_agent_run_summary
from backend.diagnosis.agents_runtime import AgentsRcaRuntimeResult
from backend.domain.agent_findings import CoordinationReview
from backend.domain.agent_plan import AgentExecution, AgentExecutionStatus
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    ModelProvider,
    MultiAgentRunStatus,
    ResultValidationCategory,
    StabilizationCategory,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
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
        "agent_config",
        "tool_calls",
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
    assert workbench["agent_config"]["provider"] == "openai"
    assert workbench["evidence"]
    assert workbench["graph_seed"]["nodes"]
    assert workbench["graph_seed"]["edges"]
    node_ids = {node["id"] for node in workbench["graph_seed"]["nodes"]}
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in workbench["graph_seed"]["edges"]
    )


def test_agent_config_and_workbench_expose_only_non_secret_provider_status(
    client: TestClient,
    investigation_id: str,
    monkeypatch,
) -> None:
    settings = get_container().settings.agents
    settings.provider = ModelProvider.DEEPSEEK
    settings.model = "deepseek-v4-pro"
    monkeypatch.setattr(
        config_api,
        "latest_certification",
        lambda *_args: "not_run",
    )

    config_response = client.get("/config/agents")
    workbench_response = client.get(
        f"/investigations/{investigation_id}/rca-workbench"
    )

    expected = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "implementation_status": "implemented",
        "certification_status": "not_run",
        "strategy": "fixed",
        "max_tool_calls_per_specialist": 3,
        "max_total_tool_calls": 8,
        "tool_timeout_seconds": 10,
    }
    assert config_response.status_code == 200
    assert config_response.json() == expected
    assert workbench_response.status_code == 200
    assert workbench_response.json()["agent_config"] == expected
    combined = f"{config_response.text}{workbench_response.text}".lower()
    assert "api_key" not in combined
    assert "deepseek_api_key" not in combined
    assert "api.deepseek.com" not in combined


def test_workbench_contains_adaptive_trace_metadata(
    client: TestClient,
    investigation_id: str,
):
    repository = get_container().repository
    record = repository.get(investigation_id)
    record.strategy = InvestigationStrategy.ADAPTIVE
    repository.save(record)
    failed = AgentsRcaRuntimeResult.failed(investigation_id, "runtime timeout")
    task = failed.tasks[0].model_copy(update={"analysis_round": 1})
    execution = failed.executions[0].model_copy(
        update={
            "task_id": task.id,
            "analysis_round": 1,
            "failure_category": FailureCategory.TIMEOUT,
            "model_provider": ModelProvider.OPENAI,
            "model_name": "gpt-test",
        }
    )
    repository.save_tasks(
        investigation_id,
        [*repository.list_tasks(investigation_id), task],
    )
    repository.save_executions(investigation_id, [execution])
    repository.save_tool_calls(
        investigation_id,
        [
            ToolCallRecord(
                task_id=task.id,
                agent_name="LogAgent",
                tool_name="read_logs",
                input={
                    "reason": "inspect incident logs",
                    "limit": 20,
                    "token": "tool-secret-value",
                },
                status=ToolCallStatus.SUCCESS,
                output_evidence_ids=[record.evidence[0].id],
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        ],
    )

    response = client.get(
        f"/investigations/{investigation_id}/rca-workbench"
    )
    body = response.json()

    assert body["multi_agent_run"]["strategy"] == "adaptive"
    assert body["multi_agent_run"]["adaptive_status"] == "degraded"
    assert body["multi_agent_run"]["tool_call_count"] == 1
    assert body["tool_calls"][0]["input"]["reason"] == "inspect incident logs"
    assert "tool-secret-value" not in response.text


def test_v8_1_workbench_exposes_persisted_reliability_fields(
    client: TestClient,
    investigation_id: str,
):
    repository = get_container().repository
    stored_review = repository.get_coordination_review(investigation_id).model_copy(
        update={
            "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            "run_status": MultiAgentRunStatus.PARTIAL,
            "model_provider": ModelProvider.OPENAI,
            "model_name": "gpt-test",
            "primary_stabilization_category": StabilizationCategory.FINAL_SYNTHESIS,
            "secondary_stabilization_categories": [
                StabilizationCategory.SPECIALIST_OUTPUT_CONTRACT
            ],
        }
    )
    execution = AgentExecution(
        id="exec-v8-1",
        task_id="task-v8-1",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.FINAL_SYNTHESIS,
        failure_category=FailureCategory.INVALID_OUTPUT,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
    )
    repository.save_multi_agent_result(
        investigation_id, [], [execution], stored_review
    )

    workbench = client.get(f"/investigations/{investigation_id}/rca-workbench").json()

    assert workbench["coordination_review"]["model_provider"] == "openai"
    assert workbench["coordination_review"]["model_name"] == "gpt-test"
    assert workbench["agent_executions"][-1]["step_kind"] == "final_synthesis"
    assert workbench["agent_executions"][-1]["failure_category"] == "invalid_output"
    assert workbench["multi_agent_run"] == {
        "status": "partial",
        "failure_reason": None,
        "model_provider": "openai",
        "model_name": "gpt-test",
        "primary_stabilization_category": "final_synthesis",
        "secondary_stabilization_categories": ["specialist_output_contract"],
    }


def test_v8_1_old_payload_defaults_remain_backward_compatible():
    old_execution = AgentExecution.model_validate(
        {
            "id": "exec-old",
            "task_id": "task-old",
            "agent_name": "CoordinatorAgent",
            "status": "failed",
        }
    )
    old_review = CoordinationReview.model_validate(
        {"id": "review-old", "investigation_id": "inv-old"}
    )

    assert old_execution.step_kind is None
    assert old_execution.attempt == 1
    assert old_execution.failure_category == FailureCategory.NONE
    assert old_execution.result_validation_category is None
    assert old_execution.model_provider is None
    assert old_execution.model_name is None
    assert old_review.model_provider is None
    assert old_review.model_name is None
    assert old_review.primary_stabilization_category is None
    assert old_review.secondary_stabilization_categories == []


def test_v8_1_workbench_exposes_safe_result_validation_category(
    client: TestClient,
    investigation_id: str,
):
    repository = get_container().repository
    result = AgentsRcaRuntimeResult.validation_failed(
        ResultValidationCategory.REVIEW_CONTRACT,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
    )
    repository.save_multi_agent_result(
        investigation_id,
        [],
        result.executions,
        None,
    )

    response = client.get(f"/investigations/{investigation_id}/rca-workbench")
    workbench = response.json()
    execution = workbench["agent_executions"][-1]

    assert response.status_code == 200
    assert execution["step_kind"] == "result_validation"
    assert execution["failure_category"] == "invalid_output"
    assert execution["result_validation_category"] == "review_contract"
    assert workbench["multi_agent_run"] == {
        "status": "failed",
        "failure_reason": "Agents runtime returned invalid output",
        "model_provider": "openai",
        "model_name": "gpt-test",
        "primary_stabilization_category": "result_validation",
        "secondary_stabilization_categories": [],
    }
    assert "raw-secret" not in response.text


def test_v8_1_skipped_summary_uses_persisted_attribution_not_environment(
    monkeypatch,
):
    monkeypatch.setenv("DIAGOPS_AGENTS_PROVIDER", "openai")
    execution = AgentExecution(
        id="exec-skipped-deepseek",
        task_id="task-skipped-deepseek",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.SKIPPED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.INITIAL_COORDINATION,
        failure_category=FailureCategory.NOT_CONFIGURED,
        model_provider=ModelProvider.DEEPSEEK,
        model_name="deepseek-test",
    )

    summary = _multi_agent_run_summary(None, [execution])

    assert summary is not None
    assert summary.model_dump(mode="json") == {
        "status": "skipped",
        "failure_reason": "Agents runtime not configured",
        "model_provider": "deepseek",
        "model_name": "deepseek-test",
        "primary_stabilization_category": "unknown",
        "secondary_stabilization_categories": [],
    }


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
        "model_provider": None,
        "model_name": None,
        "primary_stabilization_category": None,
        "secondary_stabilization_categories": [],
    }


def test_legacy_agent_views_redact_repository_objects_without_rewriting_them(
    client: TestClient,
    investigation_id: str,
) -> None:
    repository = get_container().repository
    execution = AgentExecution(
        id="exec-legacy-secret",
        task_id="task-legacy-secret",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        error_message="Bearer review-test-token",
    )
    review = repository.get_coordination_review(investigation_id).model_copy(
        update={"summary": "Bearer review-summary-token"}
    )
    repository.save_executions(investigation_id, [execution])
    repository.save_coordination_review(review)

    executions_response = client.get(
        f"/investigations/{investigation_id}/agent-executions"
    )
    review_response = client.get(
        f"/investigations/{investigation_id}/coordination-review"
    )
    workbench_response = client.get(
        f"/investigations/{investigation_id}/rca-workbench"
    )

    assert executions_response.status_code == 200
    assert review_response.status_code == 200
    assert workbench_response.status_code == 200
    assert "review-test-token" not in executions_response.text
    assert "review-summary-token" not in review_response.text
    assert "review-test-token" not in workbench_response.text
    assert "review-summary-token" not in workbench_response.text
    assert repository.list_executions(investigation_id)[-1].error_message == (
        "Bearer review-test-token"
    )
    assert repository.get_coordination_review(investigation_id).summary == (
        "Bearer review-summary-token"
    )


@pytest.mark.parametrize(
    ("status", "error_message", "failure_category", "expected_reason"),
    [
        (
            AgentExecutionStatus.FAILED,
            "provider exploded Bearer token-secret secret@example.com connection=db",
            FailureCategory.UNKNOWN,
            "Agents runtime failed",
        ),
        (
            AgentExecutionStatus.SKIPPED,
            "Agents runtime is not locally configured; Bearer token-secret secret@example.com",
            FailureCategory.NOT_CONFIGURED,
            "Agents runtime not configured",
        ),
        (
            AgentExecutionStatus.SKIPPED,
            None,
            FailureCategory.NOT_CONFIGURED,
            "Agents runtime not configured",
        ),
        (
            AgentExecutionStatus.FAILED,
            "ValidationError: invalid model output; Bearer token-secret secret@example.com",
            FailureCategory.INVALID_OUTPUT,
            "Agents runtime returned invalid output",
        ),
    ],
)
def test_workbench_derives_failed_or_skipped_sdk_run_without_replacing_v5_review(
    client: TestClient,
    investigation_id: str,
    status: AgentExecutionStatus,
    error_message: str | None,
    failure_category: FailureCategory,
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
                failure_category=failure_category,
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
                failure_category=FailureCategory.TIMEOUT,
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
                failure_category=FailureCategory.NOT_CONFIGURED,
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
        "model_provider": None,
        "model_name": None,
        "primary_stabilization_category": "cancelled_or_timeout",
        "secondary_stabilization_categories": ["unknown"],
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
                failure_category=FailureCategory.TIMEOUT,
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
        "model_provider": None,
        "model_name": None,
        "primary_stabilization_category": "cancelled_or_timeout",
        "secondary_stabilization_categories": [],
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
        failure_category=FailureCategory.TIMEOUT,
        error_message="request timeout",
        started_at=timestamp,
    )
    skipped = AgentExecution(
        id="exec-z",
        task_id="task-z",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.SKIPPED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        failure_category=FailureCategory.NOT_CONFIGURED,
        started_at=timestamp,
    )

    expected = {
        "status": MultiAgentRunStatus.SKIPPED,
        "failure_reason": "Agents runtime not configured",
        "model_provider": None,
        "model_name": None,
        "primary_stabilization_category": "cancelled_or_timeout",
        "secondary_stabilization_categories": ["unknown"],
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
        failure_category=FailureCategory.TIMEOUT,
        error_message="request timeout",
        started_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
    )
    naive_newer = AgentExecution(
        id="exec-z",
        task_id="task-z",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.SKIPPED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        failure_category=FailureCategory.NOT_CONFIGURED,
        started_at=datetime(2026, 7, 10, 10),
    )

    for executions in ([aware_older, naive_newer], [naive_newer, aware_older]):
        summary = _multi_agent_run_summary(None, executions)
        assert summary.model_dump(mode="json") == {
            "status": MultiAgentRunStatus.SKIPPED,
            "failure_reason": "Agents runtime not configured",
            "model_provider": None,
            "model_name": None,
            "primary_stabilization_category": "cancelled_or_timeout",
            "secondary_stabilization_categories": ["unknown"],
        }
