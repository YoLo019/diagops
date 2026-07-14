import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.agent_plan import AgentExecution, AgentExecutionStatus
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    ExecutionStepKind,
    FailureCategory,
    ModelProvider,
    ResultValidationCategory,
)
from backend.services.reliability_artifacts import (
    ReliabilityArtifact,
    latest_certification,
    project_execution,
    read_reliability_artifact,
    write_reliability_artifact,
)

CANONICAL_CASE_IDS = (
    "database_slowdown",
    "dependency_timeout",
    "deployment_regression",
    "single_bad_instance",
    "traffic_spike",
)
REQUIRED_THRESHOLDS = (
    "valid_results",
    "real_reviews",
    "correct_candidates",
    "per_case_accuracy",
    "reference_validity",
    "unsafe_tools",
    "injection_successes",
    "wrong_agreements",
    "agreement_contract_validity",
    "executed_action_claim_validity",
)


def _artifact(
    run_id: str,
    *,
    passed: bool = True,
    completed_at: datetime | None = None,
    provider: str = "openai",
    model: str = "gpt-test",
    mode: str = "live",
) -> ReliabilityArtifact:
    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    case_results = [
        {
            "case_id": case_id,
            "repetition": repetition,
            "provider": provider,
            "model": model,
            "real_review": True,
            "run_status": "completed",
            "primary_stabilization_category": None,
        }
        for case_id in CANONICAL_CASE_IDS
        for repetition in range(1, 4)
    ]
    return ReliabilityArtifact(
        run_id=run_id,
        mode=mode,
        provider=provider,
        model=model,
        started_at=started_at,
        completed_at=completed_at or started_at + timedelta(minutes=1),
        case_results=case_results,
        aggregate={
            "passed": passed,
            "thresholds": {
                name: {"passed": passed} for name in REQUIRED_THRESHOLDS
            },
        },
    )


def test_schema_v3_live_artifact_requires_provider_and_model(tmp_path):
    with pytest.raises(ValueError, match="provider and model required"):
        write_reliability_artifact(
            tmp_path,
            _artifact("run-live-1", provider="", model=""),
        )


def test_schema_v3_deterministic_artifact_uses_substitute(tmp_path):
    artifact = _artifact(
        "run-deterministic-1",
        mode="deterministic",
        provider="substitute",
        model="fault-injector-v1",
    )

    path = write_reliability_artifact(tmp_path, artifact)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 3
    assert payload["provider"] == "substitute"
    assert payload["model"] == "fault-injector-v1"
    assert path == tmp_path / artifact.run_id / "result.json"
    assert path.with_name("summary.md").exists()


def test_reader_rejects_future_schema_and_preserves_historical_inputs(tmp_path):
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"schema_version": 4}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported schema version"):
        read_reliability_artifact(future)

    for version in (1, 2):
        historical = tmp_path / f"historical-{version}.json"
        original = json.dumps({"schema_version": version, "legacy": True})
        historical.write_text(original, encoding="utf-8")

        assert read_reliability_artifact(historical) is None
        assert historical.read_text(encoding="utf-8") == original


def test_execution_projection_excludes_sensitive_and_free_text_fields():
    execution = AgentExecution(
        task_id="task-secret",
        agent_name="CoordinatorAgent",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        analysis_round=2,
        step_kind=ExecutionStepKind.FINAL_SYNTHESIS,
        attempt=1,
        failure_category=FailureCategory.INVALID_OUTPUT,
        result_validation_category=ResultValidationCategory.REVIEW_CONTRACT,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        evidence_ids=["ev-safe"],
        summary="raw reasoning secret-summary",
        error_message="Bearer secret-token https://api.example.invalid/v1",
    )

    payload = project_execution(execution).model_dump(mode="json")
    serialized = json.dumps(payload)

    assert set(payload) == {
        "step_kind",
        "agent_name",
        "analysis_round",
        "attempt",
        "status",
        "failure_category",
        "result_validation_category",
        "evidence_ids",
    }
    assert payload["step_kind"] == "final_synthesis"
    assert payload["result_validation_category"] == "review_contract"
    assert "secret" not in serialized
    assert "error_message" not in serialized
    assert "task-secret" not in serialized
    assert "gpt-test" not in serialized
    assert "https://" not in serialized


def test_latest_certification_returns_not_run_without_matching_live_artifact(
    tmp_path,
):
    write_reliability_artifact(
        tmp_path,
        _artifact(
            "deterministic",
            mode="deterministic",
            provider="substitute",
            model="fault-injector-v1",
        ),
    )
    write_reliability_artifact(
        tmp_path,
        _artifact("other-provider", provider="deepseek", model="deepseek-test"),
    )

    assert latest_certification(tmp_path, "openai", "gpt-test") == "not_run"


def test_latest_certification_uses_completed_at_then_run_id(tmp_path):
    completed_at = datetime(2026, 7, 12, 1, tzinfo=UTC)
    write_reliability_artifact(
        tmp_path,
        _artifact("run-a", passed=True, completed_at=completed_at),
    )
    write_reliability_artifact(
        tmp_path,
        _artifact("run-b", passed=False, completed_at=completed_at),
    )

    assert latest_certification(tmp_path, "openai", "gpt-test") == "failed"


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_rows",
        "duplicate_row",
        "row_attribution",
        "unclassified_row",
        "missing_threshold",
        "inconsistent_aggregate",
    ],
)
def test_latest_certification_ignores_noncanonical_live_artifacts(
    tmp_path,
    mutation,
):
    artifact = _artifact(f"invalid-{mutation}")
    if mutation == "empty_rows":
        artifact.case_results = []
    elif mutation == "duplicate_row":
        artifact.case_results[-1] = dict(artifact.case_results[0])
    elif mutation == "row_attribution":
        artifact.case_results[0]["provider"] = "deepseek"
    elif mutation == "unclassified_row":
        artifact.case_results[0].update(
            {
                "real_review": False,
                "run_status": "failed",
                "primary_stabilization_category": None,
            }
        )
    elif mutation == "missing_threshold":
        del artifact.aggregate["thresholds"]["reference_validity"]
    else:
        artifact.aggregate["passed"] = False
    write_reliability_artifact(tmp_path, artifact)

    assert latest_certification(tmp_path, "openai", "gpt-test") == "not_run"


def test_latest_certification_ignores_malformed_file_without_hiding_valid_result(
    tmp_path,
):
    write_reliability_artifact(
        tmp_path,
        _artifact("valid-run", passed=True),
    )
    malformed = tmp_path / "newer-malformed" / "result.json"
    malformed.parent.mkdir()
    malformed.write_text("{not-json", encoding="utf-8")

    assert latest_certification(tmp_path, "openai", "gpt-test") == "certified"


def test_latest_certification_accepts_explicit_classification_gate_failure(
    tmp_path,
):
    artifact = _artifact("classification-failed")
    artifact.case_results[0].update(
        {
            "real_review": False,
            "run_status": "failed",
            "primary_stabilization_category": None,
        }
    )
    artifact.aggregate["thresholds"]["failure_classification"] = {
        "passed": False
    }
    artifact.aggregate["passed"] = False
    write_reliability_artifact(tmp_path, artifact)

    assert latest_certification(tmp_path, "openai", "gpt-test") == "failed"
