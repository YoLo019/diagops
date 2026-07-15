import json
from datetime import UTC, datetime

import pytest

from backend.benchmarks.openrca.evaluator import _update_summary, evaluate_prediction
from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaStrategySummary,
)


def test_evaluator_matches_component_reason_and_one_minute_time_tolerance():
    scoring = (
        "The only predicted root cause component is checkoutservice\n"
        "The only predicted root cause reason is cpu exhausted\n"
        "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2026-07-14 12:00:00"
    )
    prediction = json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:00:45",
                "root cause component": "checkoutservice",
                "root cause reason": "cpu exhausted",
            }
        }
    )

    score = evaluate_prediction(prediction, scoring)

    assert score.partial_score == 1.0
    assert score.strict is True
    assert score.component_score == 1.0
    assert score.reason_score == 1.0
    assert score.time_score == 1.0


def test_evaluator_uses_best_permutation_for_multiple_failures():
    scoring = (
        "The 1-th predicted root cause component is checkoutservice\n"
        "The 1-th predicted root cause reason is cpu exhausted\n"
        "The 1-th root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2026-07-14 12:00:00\n"
        "The 2-th predicted root cause component is paymentservice\n"
        "The 2-th predicted root cause reason is dependency errors\n"
        "The 2-th root cause occurrence time is within 1 minutes (i.e., <=1min) of "
        "2026-07-14 12:05:00"
    )
    prediction = json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:05:00",
                "root cause component": "paymentservice",
                "root cause reason": "dependency errors",
            },
            "2": {
                "root cause occurrence datetime": "2026-07-14 12:00:00",
                "root cause component": "checkoutservice",
                "root cause reason": "cpu exhausted",
            },
        }
    )

    score = evaluate_prediction(prediction, scoring)

    assert score.strict is True
    assert score.partial_score == 1.0


def test_evaluator_scores_zero_when_prediction_count_differs_from_ground_truth():
    scoring = (
        "The 1-th predicted root cause component is checkoutservice\n"
        "The 2-th predicted root cause component is paymentservice"
    )
    prediction = json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:00:00",
                "root cause component": "checkoutservice",
                "root cause reason": "cpu exhausted",
            }
        }
    )

    score = evaluate_prediction(prediction, scoring)

    assert score.partial_score == 0.0
    assert score.strict is False


def test_evaluator_preserves_official_exact_component_comparison():
    prediction = json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:00:00",
                "root cause component": " checkoutservice",
                "root cause reason": "cpu exhausted",
            }
        }
    )

    score = evaluate_prediction(
        prediction,
        "The only predicted root cause component is checkoutservice",
    )

    assert score.partial_score == 0.0


def test_summary_field_accuracy_excludes_unscored_criteria(tmp_path):
    strategy = OpenRcaStrategySummary(
        case_count=2,
        completed_count=2,
        completion_rate=1,
        evidence_reference_validity=1,
        invalid_evidence_references=0,
        read_only_violations=0,
        average_tool_calls=0,
        duplicate_query_rejections=0,
        average_duration_ms=1,
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0,
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        OpenRcaBenchmarkSummary(
            run_id="run-test",
            case_count=2,
            model="test-model",
            prompt_version="test-prompt",
            git_commit="test-commit",
            started_at=datetime(2026, 7, 14, tzinfo=UTC),
            completed_at=datetime(2026, 7, 14, 0, 1, tzinfo=UTC),
            strategies={"fixed": strategy},
        ).model_dump_json(),
        encoding="utf-8",
    )
    rows = [
        {
            "strategy": "fixed",
            "partition": "Bank",
            "strict": 1,
            "partial_score": 1.0,
            "component_score": 0.0,
            "component_points": 0,
            "reason_score": 0.0,
            "reason_points": 0,
            "time_score": 1.0,
            "time_points": 1,
        },
        {
            "strategy": "fixed",
            "partition": "Bank",
            "strict": 1,
            "partial_score": 1.0,
            "component_score": 1.0,
            "component_points": 1,
            "reason_score": 0.0,
            "reason_points": 0,
            "time_score": 0.0,
            "time_points": 0,
        },
    ]

    _update_summary(summary_path, rows)

    updated = OpenRcaBenchmarkSummary.model_validate_json(
        summary_path.read_text(encoding="utf-8")
    )
    assert updated.strategies["fixed"].component_score == 1.0
    assert updated.strategies["fixed"].time_score == 1.0


@pytest.mark.parametrize("prediction", ["not-json", "{}", "[]"])
def test_evaluator_returns_zero_for_invalid_prediction(prediction: str):
    score = evaluate_prediction(
        prediction,
        "The only predicted root cause component is checkoutservice",
    )

    assert score.strict is False
    assert score.partial_score == 0.0
