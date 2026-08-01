import csv
import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import backend.benchmarks.openrca.evaluator as openrca_evaluator
from backend.benchmarks.openrca.evaluator import (
    _update_summary,
    evaluate_prediction,
    gate_run,
    paired_gate_run,
    targeted_gate_run,
    write_official_query_inputs,
)
from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaPairedComparison,
    OpenRcaRuntimeIndex,
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


_PAIRED_PARTITIONS = ("Bank", "Telecom", "Market/cloudbed-1", "Market/cloudbed-2")
_PREDICTION_FIELDS = ("case_id", "partition", "row_id", "task_index", "prediction", "metadata")
_OFFICIAL_FIELDS = ("query", "answer", "groundtruth", "passed", "failed", "score", "task_index")


def _csv_bytes(fieldnames, rows) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _paired_case_dicts(clone_first_pair: bool = False) -> list[dict]:
    cases = []
    for partition in _PAIRED_PARTITIONS:
        for row in range(10):
            cases.append(
                {
                    "case_id": f"{partition}:{row}",
                    "partition": partition,
                    "row_id": str(row),
                    "task_index": "task_1",
                    "instruction": (
                        f"A failure occurred in {partition} case {row} on July 14, 2026, "
                        "within the time range of 12:00 to 12:10. Diagnose it."
                    ),
                    "expected_root_cause_count": 1,
                    "start_time": "2026-07-14T12:00:00+08:00",
                    "end_time": "2026-07-14T12:10:00+08:00",
                    "telemetry_dir": f"{partition}/telemetry/2026_07_14",
                }
            )
    if clone_first_pair:
        cases[1]["instruction"] = cases[0]["instruction"]
    return cases


def _paired_prediction(case_id: str) -> str:
    return json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:00:00",
                "root cause component": f"comp-{case_id}",
                "root cause reason": "failure",
            }
        },
        ensure_ascii=False,
    )


def _refresh_paired_eval_manifest(evaluation_dir: Path, base_manifest: dict | None = None) -> None:
    manifest_path = evaluation_dir / "run-manifest.json"
    if base_manifest is None:
        base_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = dict(base_manifest)
    manifest["artifact_checksums"] = {
        name: hashlib.sha256((evaluation_dir / name).read_bytes()).hexdigest()
        for name in ("fixed-predictions.csv", "official-report.csv", "summary.json")
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _paired_summary(label: str, **fixed_overrides) -> bytes:
    fixed = {
        "case_count": 40,
        "completed_count": 40,
        "completion_rate": 1,
        "evidence_reference_validity": 1,
        "invalid_evidence_references": 0,
        "read_only_violations": 0,
        "average_tool_calls": 0,
        "duplicate_query_rejections": 0,
        "average_duration_ms": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0,
    }
    fixed.update(fixed_overrides)
    summary = OpenRcaBenchmarkSummary(
        run_id=f"run-{label}",
        case_count=40,
        model="deterministic",
        prompt_version="v10-shared-core",
        git_commit=f"{label}-commit",
        started_at=datetime(2026, 7, 14, tzinfo=UTC),
        completed_at=datetime(2026, 7, 14, 1, tzinfo=UTC),
        strategies={"fixed": OpenRcaStrategySummary(**fixed)},
    )
    return (summary.model_dump_json(indent=2) + "\n").encode("utf-8")


def _write_paired_side(
    root: Path,
    label: str,
    cases: list[dict],
    predictions: dict[str, str],
    *,
    official_score: str,
    **summary_overrides,
) -> tuple[Path, Path]:
    run_dir = root / f"{label}-run"
    evaluation_dir = root / f"{label}-eval"
    run_dir.mkdir()
    evaluation_dir.mkdir()
    manifest = {
        "run_id": f"run-{label}",
        "case_manifest_hash": "paired-hash",
        "model": "deterministic",
        "provider": None,
        "mode": "deterministic",
        "prompt_version": "v10-shared-core",
        "git_commit": f"{label}-commit",
        "strategies": ["fixed"],
        "strategy_config": {
            "max_rounds": 2,
            "max_tool_calls_per_specialist": 3,
            "max_total_tool_calls": 8,
            "timeout_seconds": 60,
        },
        "input_cost_per_million": 0,
        "output_cost_per_million": 0,
        "started_at": "2026-07-14T00:00:00+00:00",
        "completed_at": "2026-07-14T01:00:00+00:00",
    }
    prediction_rows = [
        {
            "case_id": case["case_id"],
            "partition": case["partition"],
            "row_id": case["row_id"],
            "task_index": case["task_index"],
            "prediction": predictions[case["case_id"]],
            "metadata": json.dumps({"expected_root_cause_count": 1}),
        }
        for case in cases
    ]
    predictions_bytes = _csv_bytes(_PREDICTION_FIELDS, prediction_rows)
    summary_bytes = _paired_summary(label, **summary_overrides)
    # execution bundle 是 pre-eval 状态：manifest 不含 artifact_checksums。
    (run_dir / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "fixed-predictions.csv").write_bytes(predictions_bytes)
    (run_dir / "summary.json").write_bytes(summary_bytes)
    (evaluation_dir / "fixed-predictions.csv").write_bytes(predictions_bytes)
    (evaluation_dir / "summary.json").write_bytes(summary_bytes)
    official_rows = [
        {
            "query": case["instruction"],
            "answer": predictions[case["case_id"]],
            "groundtruth": "",
            "passed": "",
            "failed": "",
            "score": official_score,
            "task_index": case["task_index"],
        }
        for case in cases
    ]
    (evaluation_dir / "official-report.csv").write_bytes(
        _csv_bytes(_OFFICIAL_FIELDS, official_rows)
    )
    _refresh_paired_eval_manifest(evaluation_dir, manifest)
    return run_dir, evaluation_dir


def _write_paired_fixture(tmp_path: Path, *, clone_first_pair: bool = False) -> dict[str, Path]:
    cases = _paired_case_dicts(clone_first_pair)
    index = OpenRcaRuntimeIndex.model_validate(
        {"case_manifest_hash": "paired-hash", "cases": cases}
    )
    safe_index = tmp_path / "runtime-cases.json"
    safe_index.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
    predictions = {case["case_id"]: _paired_prediction(case["case_id"]) for case in cases}
    if clone_first_pair:
        predictions[cases[1]["case_id"]] = predictions[cases[0]["case_id"]]
    baseline_run, baseline_eval = _write_paired_side(
        tmp_path,
        "baseline",
        cases,
        predictions,
        official_score="0.2",
        strict_accuracy=0.2,
        partial_score=0.2,
        component_score=0.2,
        reason_score=0.2,
        time_score=0.2,
    )
    candidate_run, candidate_eval = _write_paired_side(
        tmp_path,
        "candidate",
        cases,
        predictions,
        official_score="0.25",
        strict_accuracy=0.3,
        partial_score=0.25,
        component_score=0.3,
        reason_score=0.3,
        time_score=0.3,
    )
    return {
        "safe_index": safe_index,
        "baseline_run_dir": baseline_run,
        "baseline_evaluation_dir": baseline_eval,
        "candidate_run_dir": candidate_run,
        "candidate_evaluation_dir": candidate_eval,
        "official_evaluator_root": tmp_path / "official-evaluator",
        "output_path": tmp_path / "comparison.json",
    }


def _init_evaluator_repo(path: Path) -> str:
    path.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init")
    git("config", "user.email", "paired-gate@test.local")
    git("config", "user.name", "paired-gate-test")
    (path / "evaluate.py").write_text("# official evaluator fixture\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "freeze official evaluator")
    return git("rev-parse", "HEAD")


@pytest.fixture
def paired_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = _write_paired_fixture(tmp_path)
    head = _init_evaluator_repo(paths["official_evaluator_root"])
    monkeypatch.setattr(openrca_evaluator, "APPROVED_OFFICIAL_EVALUATOR_COMMIT", head)
    return paths


def _rewrite_official_report(evaluation_dir: Path, rows: list[dict]) -> None:
    (evaluation_dir / "official-report.csv").write_bytes(_csv_bytes(_OFFICIAL_FIELDS, rows))
    _refresh_paired_eval_manifest(evaluation_dir)


def _read_official_rows(evaluation_dir: Path) -> list[dict]:
    with (evaluation_dir / "official-report.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _rewrite_eval_summary(evaluation_dir: Path, **fixed_overrides) -> None:
    summary_path = evaluation_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["strategies"]["fixed"].update(fixed_overrides)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _refresh_paired_eval_manifest(evaluation_dir)


def test_paired_gate_accepts_decimal_boundary_and_writes_comparison(paired_paths):
    assert paired_gate_run(**paired_paths) == []

    comparison = OpenRcaPairedComparison.model_validate_json(
        paired_paths["output_path"].read_text(encoding="utf-8")
    )
    assert comparison.passed is True
    assert comparison.failures == []
    assert comparison.official_partial_delta == "0.05"
    assert comparison.baseline.official_partial_score == "0.2"
    assert comparison.candidate.official_partial_score == "0.25"
    assert comparison.compatible_strict_delta == "0.1"
    assert comparison.compatible_component_delta == "0.1"
    assert comparison.baseline.run_id == "run-baseline"
    assert comparison.candidate.run_id == "run-candidate"
    assert comparison.baseline.source_sha == "baseline-commit"
    assert comparison.official_evaluator_commit != ""
    expected_sha = hashlib.sha256(
        (paired_paths["baseline_run_dir"] / "run-manifest.json").read_bytes()
    ).hexdigest()
    assert comparison.baseline.pre_eval_manifest_sha256 == expected_sha
    assert comparison.baseline.execution_artifact_sha256["fixed-predictions.csv"]
    assert comparison.candidate.evaluation_artifact_sha256["official-report.csv"]


def test_paired_gate_rejects_prediction_byte_mismatch(paired_paths):
    evaluation_predictions = paired_paths["baseline_evaluation_dir"] / "fixed-predictions.csv"
    evaluation_predictions.write_bytes(
        evaluation_predictions.read_bytes().replace(b"comp-Bank:0", b"comp-Bank:X")
    )
    _refresh_paired_eval_manifest(paired_paths["baseline_evaluation_dir"])

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "byte-for-byte" in failures[0]


def test_paired_gate_rejects_manifest_field_tamper(paired_paths):
    evaluation_dir = paired_paths["baseline_evaluation_dir"]
    manifest_path = evaluation_dir / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["git_commit"] = "tampered-commit"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "manifest" in failures[0]


def test_paired_gate_rejects_prediction_case_set_mismatch(paired_paths):
    for key in ("baseline_run_dir", "baseline_evaluation_dir"):
        prediction_path = paired_paths[key] / "fixed-predictions.csv"
        with prediction_path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        rows[0]["case_id"] = "Bank:999"
        prediction_path.write_bytes(_csv_bytes(_PREDICTION_FIELDS, rows))
    _refresh_paired_eval_manifest(paired_paths["baseline_evaluation_dir"])

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "case set" in failures[0]


def test_paired_gate_rejects_official_answer_mismatch(paired_paths):
    evaluation_dir = paired_paths["candidate_evaluation_dir"]
    rows = _read_official_rows(evaluation_dir)
    rows[0]["answer"] = json.dumps(
        {
            "1": {
                "root cause occurrence datetime": "2026-07-14 12:00:00",
                "root cause component": "other-component",
                "root cause reason": "failure",
            }
        }
    )
    _rewrite_official_report(evaluation_dir, rows)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "multiset" in failures[0]


def test_paired_gate_rejects_duplicate_count_mismatch(tmp_path, monkeypatch):
    paths = _write_paired_fixture(tmp_path, clone_first_pair=True)
    head = _init_evaluator_repo(paths["official_evaluator_root"])
    monkeypatch.setattr(openrca_evaluator, "APPROVED_OFFICIAL_EVALUATOR_COMMIT", head)
    evaluation_dir = paths["candidate_evaluation_dir"]
    rows = _read_official_rows(evaluation_dir)
    # 两行共享同一 (query, answer)；交换计数后不同 pair 集合不变，仅 multiset 计数错配。
    rows[1] = {**rows[2], "score": rows[1]["score"]}
    _rewrite_official_report(evaluation_dir, rows)

    failures = paired_gate_run(**paths)

    assert len(failures) == 1
    assert "multiset" in failures[0]


def test_paired_gate_rejects_unapproved_evaluator_commit(paired_paths, monkeypatch):
    monkeypatch.setattr(openrca_evaluator, "APPROVED_OFFICIAL_EVALUATOR_COMMIT", "0" * 40)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "HEAD" in failures[0]


def test_paired_gate_rejects_dirty_evaluator(paired_paths):
    evaluate_py = paired_paths["official_evaluator_root"] / "evaluate.py"
    evaluate_py.write_text("# dirty change\n", encoding="utf-8")

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "clean" in failures[0]


@pytest.mark.parametrize("score", ["NaN", "Infinity"])
def test_paired_gate_rejects_non_finite_official_score(paired_paths, score):
    evaluation_dir = paired_paths["baseline_evaluation_dir"]
    rows = _read_official_rows(evaluation_dir)
    rows[0]["score"] = score
    _rewrite_official_report(evaluation_dir, rows)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "finite" in failures[0]


@pytest.mark.parametrize("row_count", [39, 41])
def test_paired_gate_rejects_official_row_count(paired_paths, row_count):
    evaluation_dir = paired_paths["baseline_evaluation_dir"]
    rows = _read_official_rows(evaluation_dir)
    if row_count < 40:
        rows = rows[:row_count]
    else:
        rows = rows + [rows[0]]
    _rewrite_official_report(evaluation_dir, rows)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "40" in failures[0]


def test_paired_gate_requires_candidate_single_run_gate(paired_paths):
    _rewrite_eval_summary(paired_paths["candidate_evaluation_dir"], read_only_violations=1)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "candidate single-run gate" in failures[0]
    assert "read-only" in failures[0]


def test_paired_gate_rejects_candidate_projection_failures(paired_paths):
    _rewrite_eval_summary(paired_paths["candidate_evaluation_dir"], projection_errors=1)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "projection" in failures[0]


def test_paired_gate_rejects_strict_regression(paired_paths):
    _rewrite_eval_summary(paired_paths["candidate_evaluation_dir"], strict_accuracy=0.1)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "strict" in failures[0]


@pytest.mark.parametrize(
    "field",
    ["component_score", "reason_score", "time_score"],
)
def test_paired_gate_rejects_compatible_field_regression(paired_paths, field):
    _rewrite_eval_summary(paired_paths["candidate_evaluation_dir"], **{field: 0.15})

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert field.removesuffix("_score") in failures[0]


def test_paired_gate_rejects_insufficient_official_delta(paired_paths):
    evaluation_dir = paired_paths["candidate_evaluation_dir"]
    rows = _read_official_rows(evaluation_dir)
    for row in rows:
        row["score"] = "0.2499"
    _rewrite_official_report(evaluation_dir, rows)

    failures = paired_gate_run(**paired_paths)

    assert len(failures) == 1
    assert "0.05" in failures[0]


@pytest.mark.parametrize(
    "key",
    [
        "baseline_run_dir",
        "baseline_evaluation_dir",
        "candidate_run_dir",
        "candidate_evaluation_dir",
    ],
)
def test_paired_gate_rejects_output_inside_checked_directories(paired_paths, key):
    kwargs = dict(paired_paths)
    kwargs["output_path"] = paired_paths[key] / "comparison.json"

    failures = paired_gate_run(**kwargs)

    assert len(failures) == 1
    assert "outside" in failures[0]
    assert not kwargs["output_path"].exists()


def test_paired_gate_persists_failed_comparison(paired_paths, monkeypatch):
    monkeypatch.setattr(openrca_evaluator, "APPROVED_OFFICIAL_EVALUATOR_COMMIT", "0" * 40)

    failures = paired_gate_run(**paired_paths)

    assert failures
    comparison = OpenRcaPairedComparison.model_validate_json(
        paired_paths["output_path"].read_text(encoding="utf-8")
    )
    assert comparison.passed is False
    assert comparison.failures == failures
    assert comparison.official_partial_delta == "0.05"
