import json

import pytest

from backend.benchmarks.openrca.evaluator import evaluate_prediction


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
        "The predicted root cause component is checkoutservice\n"
        "The predicted root cause reason is cpu exhausted\n"
        "The root cause occurrence time is within 1 minutes of 2026-07-14 12:00:00\n"
        "The predicted root cause component is paymentservice\n"
        "The predicted root cause reason is dependency errors\n"
        "The root cause occurrence time is within 1 minutes of 2026-07-14 12:05:00"
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


@pytest.mark.parametrize("prediction", ["not-json", "{}", "[]"])
def test_evaluator_returns_zero_for_invalid_prediction(prediction: str):
    score = evaluate_prediction(
        prediction,
        "The only predicted root cause component is checkoutservice",
    )

    assert score.strict is False
    assert score.partial_score == 0.0
