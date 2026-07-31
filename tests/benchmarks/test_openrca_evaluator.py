import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.benchmarks.openrca.evaluator import (
    _update_summary,
    evaluate_prediction,
    gate_run,
    targeted_gate_run,
    write_official_query_inputs,
)
from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaStrategySummary,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "openrca"


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

    updated = OpenRcaBenchmarkSummary.model_validate_json(summary_path.read_text(encoding="utf-8"))
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


def _write_gate_fixture(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    strategy = OpenRcaStrategySummary(
        case_count=40,
        completed_count=40,
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
        strict_accuracy=0,
        partial_score=0.2,
        component_score=0.1,
        reason_score=0.1,
        time_score=0.1,
    )
    (run_dir / "summary.json").write_text(
        OpenRcaBenchmarkSummary(
            run_id="run-test",
            case_count=40,
            model="deterministic",
            prompt_version="v10-shared-core",
            git_commit="test",
            started_at=datetime(2026, 7, 14, tzinfo=UTC),
            completed_at=datetime(2026, 7, 14, 0, 1, tzinfo=UTC),
            strategies={"fixed": strategy},
        ).model_dump_json(),
        encoding="utf-8",
    )
    with (run_dir / "fixed-predictions.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "case_id",
                "partition",
                "row_id",
                "task_index",
                "prediction",
                "metadata",
            ),
        )
        writer.writeheader()
        for index in range(40):
            writer.writerow(
                {
                    "case_id": f"Bank:{index}",
                    "partition": "Bank",
                    "row_id": str(index),
                    "task_index": "task_1",
                    "prediction": json.dumps(
                        {
                            "1": {
                                "root cause occurrence datetime": ("2026-07-14 12:00:00"),
                                "root cause component": "bank-api",
                                "root cause reason": "failure",
                            }
                        }
                    ),
                    "metadata": json.dumps({"expected_root_cause_count": 1}),
                }
            )
    with (run_dir / "official-report.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("score",))
        writer.writeheader()
        writer.writerows({"score": 0.2} for _ in range(40))
    checksummed = (
        "fixed-predictions.csv",
        "official-report.csv",
        "summary.json",
    )
    (run_dir / "run-manifest.json").write_text(
        json.dumps(
            {
                "mode": "deterministic",
                "strategies": ["fixed"],
                "artifact_checksums": {
                    name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
                    for name in checksummed
                },
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_release_gate_accepts_passing_deterministic_fixed_run(tmp_path):
    assert gate_run(_write_gate_fixture(tmp_path)) == []


def test_release_gate_reports_frozen_artifact_failures(tmp_path):
    run_dir = _write_gate_fixture(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    fixed = summary["strategies"]["fixed"]
    fixed.update(
        {
            "component_score": 0,
            "reason_score": 0,
            "time_score": 0,
            "evidence_reference_validity": 0.9,
            "invalid_evidence_references": 1,
            "read_only_violations": 1,
            "input_tokens": 1,
            "estimated_cost": 7,
        }
    )
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    prediction_path = run_dir / "fixed-predictions.csv"
    rows = list(csv.DictReader(prediction_path.open(encoding="utf-8")))
    rows[0]["prediction"] = "{}"
    rows[1]["prediction"] = "{}"
    rows[2]["prediction"] = "{}"
    rows[3]["metadata"] = json.dumps({"expected_root_cause_count": 2})
    with prediction_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    with (run_dir / "official-report.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("score",))
        writer.writeheader()
        writer.writerows({"score": 0} for _ in range(40))

    failures = gate_run(run_dir)

    assert any("official Fixed partial" in item for item in failures)
    assert any("component" in item for item in failures)
    assert any("cardinality" in item for item in failures)
    assert any("non-empty" in item for item in failures)
    assert any("evidence" in item for item in failures)
    assert any("read-only" in item for item in failures)
    assert any("cost" in item for item in failures)
    assert any("tokens" in item for item in failures)


def test_release_gate_rejects_changed_frozen_prediction(tmp_path):
    run_dir = _write_gate_fixture(tmp_path)
    with (run_dir / "fixed-predictions.csv").open("a", encoding="utf-8") as file:
        file.write("\n")

    assert any("checksum" in item for item in gate_run(run_dir))


def test_official_query_writer_skips_partitions_absent_from_targeted_run(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with (run_dir / "fixed-Bank.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=("row_id",))
        writer.writeheader()
        writer.writerow({"row_id": "0"})

    written = write_official_query_inputs(
        FIXTURE_ROOT,
        run_dir,
        tmp_path / "queries",
    )

    assert [path.name for path in written] == ["Bank-query.csv"]


def _write_targeted_gate_fixture(tmp_path, mutation: str | None = None):
    run_dir = tmp_path / "targeted"
    run_dir.mkdir()
    projection_errors = int(mutation == "projection_error")
    projection_fallbacks = int(mutation == "projection_fallback")
    evidence_validity = 0.9 if mutation == "invalid_evidence" else 1
    invalid_evidence = int(mutation == "invalid_evidence")
    read_only_violations = int(mutation == "read_only_violation")
    strategy = OpenRcaStrategySummary(
        case_count=6,
        completed_count=6,
        completion_rate=1,
        evidence_reference_validity=evidence_validity,
        invalid_evidence_references=invalid_evidence,
        read_only_violations=read_only_violations,
        average_tool_calls=0,
        duplicate_query_rejections=0,
        average_duration_ms=1,
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0,
        strict_accuracy=0,
        partial_score=0.5,
        component_score=0.5,
        reason_score=0.5,
        time_score=0.5,
        projection_errors=projection_errors,
        projection_fallbacks=projection_fallbacks,
    )
    (run_dir / "summary.json").write_text(
        OpenRcaBenchmarkSummary(
            run_id="run-targeted",
            case_count=6,
            model="deterministic",
            prompt_version="v10-shared-core",
            git_commit="test",
            started_at=datetime(2026, 7, 14, tzinfo=UTC),
            completed_at=datetime(2026, 7, 14, 0, 1, tzinfo=UTC),
            strategies={"fixed": strategy},
        ).model_dump_json(),
        encoding="utf-8",
    )
    tasks = ["task_1", "task_2", "task_3", "task_1", "task_2", "task_3"]
    with (run_dir / "fixed-predictions.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "case_id",
                "partition",
                "row_id",
                "task_index",
                "prediction",
                "metadata",
            ),
        )
        writer.writeheader()
        for index, task_index in enumerate(tasks):
            projection = {
                "rule_version": "v2",
                "scored_fields": ["time"],
                "selected_evidence_ids": [[f"ev-{index}"]],
                "projection_fallback": False,
                "fallback_reason": None,
                "projection_error": False,
            }
            writer.writerow(
                {
                    "case_id": f"Bank:{index}",
                    "partition": "Bank",
                    "row_id": str(index),
                    "task_index": task_index,
                    "prediction": json.dumps(
                        {
                            "1": {
                                "root cause occurrence datetime": (
                                    "2026-07-14 12:00:00"
                                ),
                                "root cause component": "bank-api",
                                "root cause reason": "failure",
                            }
                        }
                    ),
                    "metadata": json.dumps(
                        {
                            "expected_root_cause_count": 1,
                            "projection": projection,
                        }
                    ),
                }
            )
    scores = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    if mutation == "two_positive_scores":
        scores[0] = 0
    elif mutation == "missing_time_hit":
        scores[0] = scores[3] = 0
    elif mutation == "missing_reason_hit":
        scores[1] = scores[4] = 0
    elif mutation == "missing_component_hit":
        scores[2] = scores[5] = 0
    with (run_dir / "official-report.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=("task_index", "score"))
        writer.writeheader()
        writer.writerows(
            {"task_index": task_index, "score": score}
            for task_index, score in zip(tasks, scores, strict=True)
        )
    return run_dir


def test_targeted_gate_accepts_three_dimensions_and_three_of_six(tmp_path):
    assert targeted_gate_run(_write_targeted_gate_fixture(tmp_path)) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("two_positive_scores", "at least 3 of 6"),
        ("missing_time_hit", "task_1"),
        ("missing_reason_hit", "task_2"),
        ("missing_component_hit", "task_3"),
        ("projection_error", "projection errors"),
        ("projection_fallback", "projection fallbacks"),
        ("invalid_evidence", "evidence reference"),
        ("read_only_violation", "read-only"),
    ],
)
def test_targeted_gate_reports_each_blocker(tmp_path, mutation, expected):
    run_dir = _write_targeted_gate_fixture(tmp_path, mutation=mutation)

    assert any(expected in item for item in targeted_gate_run(run_dir))
