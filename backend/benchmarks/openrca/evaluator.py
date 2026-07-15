from __future__ import annotations

import csv
import itertools
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from backend.benchmarks.openrca.models import OpenRcaBenchmarkSummary

_COMPONENT_PATTERN = re.compile(
    r"root cause component is\s+([^\r\n]+)", re.IGNORECASE
)
_REASON_PATTERN = re.compile(r"root cause reason is\s+([^\r\n]+)", re.IGNORECASE)
_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class EvaluationScore:
    strict: bool
    partial_score: float
    component_score: float
    reason_score: float
    time_score: float


@dataclass(frozen=True)
class _Cause:
    occurred_at: datetime | None
    component: str | None
    reason: str | None


def evaluate_prediction(prediction: str, scoring_points: str) -> EvaluationScore:
    predicted = _parse_prediction(prediction)
    expected = _parse_scoring_points(scoring_points)
    if not predicted or not expected:
        return EvaluationScore(False, 0.0, 0.0, 0.0, 0.0)

    best = (0, 0, 0)
    for ordering in itertools.permutations(predicted):
        component = reason = occurrence = 0
        for actual, target in zip(ordering, expected, strict=False):
            component += int(
                target.component is not None and actual.component == target.component
            )
            reason += int(target.reason is not None and actual.reason == target.reason)
            occurrence += int(
                target.occurred_at is not None
                and actual.occurred_at is not None
                and abs(actual.occurred_at - target.occurred_at)
                <= timedelta(minutes=1)
            )
        best = max(best, (component, reason, occurrence), key=sum)

    expected_components = sum(item.component is not None for item in expected)
    expected_reasons = sum(item.reason is not None for item in expected)
    expected_times = sum(item.occurred_at is not None for item in expected)
    denominator = expected_components + expected_reasons + expected_times
    total_matches = sum(best)
    strict = len(predicted) == len(expected) and total_matches == denominator
    return EvaluationScore(
        strict=strict,
        partial_score=total_matches / denominator if denominator else 0.0,
        component_score=best[0] / expected_components if expected_components else 0.0,
        reason_score=best[1] / expected_reasons if expected_reasons else 0.0,
        time_score=best[2] / expected_times if expected_times else 0.0,
    )


def evaluate_run(query_root: Path, run_dir: Path) -> Path:
    scoring = _load_scoring_points(query_root)
    report_rows: list[dict[str, object]] = []
    for strategy in ("fixed", "adaptive"):
        prediction_path = run_dir / f"{strategy}-predictions.csv"
        if not prediction_path.exists():
            continue
        with prediction_path.open(
            encoding="utf-8", newline=""
        ) as file:
            predictions = list(csv.DictReader(file))
        for row in predictions:
            key = (row["partition"], row["task_index"])
            score = evaluate_prediction(row["prediction"], scoring.get(key, ""))
            report_rows.append(
                {
                    "strategy": strategy,
                    "case_id": row["case_id"],
                    "partition": row["partition"],
                    "strict": int(score.strict),
                    "partial_score": score.partial_score,
                    "component_score": score.component_score,
                    "reason_score": score.reason_score,
                    "time_score": score.time_score,
                }
            )
    report_path = run_dir / "compatible-report.csv"
    _write_report(report_path, report_rows)
    _update_summary(run_dir / "summary.json", report_rows)
    return report_path


def _parse_prediction(value: str) -> list[_Cause]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not payload:
        return []
    causes = []
    for index, item in enumerate(payload.values(), start=1):
        if str(index) not in payload or not isinstance(item, dict):
            return []
        occurred_at = item.get("root cause occurrence datetime")
        component = item.get("root cause component")
        reason = item.get("root cause reason")
        if not all(isinstance(field, str) for field in (occurred_at, component, reason)):
            return []
        try:
            parsed_time = datetime.strptime(occurred_at, _DATETIME_FORMAT)
        except ValueError:
            return []
        causes.append(_Cause(parsed_time, component.strip(), reason.strip()))
    return causes


def _parse_scoring_points(value: str) -> list[_Cause]:
    components = [item.strip() for item in _COMPONENT_PATTERN.findall(value)]
    reasons = [item.strip() for item in _REASON_PATTERN.findall(value)]
    times = [datetime.strptime(item, _DATETIME_FORMAT) for item in _TIME_PATTERN.findall(value)]
    count = max(len(components), len(reasons), len(times))
    return [
        _Cause(
            times[index] if index < len(times) else None,
            components[index] if index < len(components) else None,
            reasons[index] if index < len(reasons) else None,
        )
        for index in range(count)
    ]


def _load_scoring_points(query_root: Path) -> dict[tuple[str, str], str]:
    result = {}
    for partition in ("Bank", "Telecom", "Market/cloudbed-1", "Market/cloudbed-2"):
        with (query_root / partition / "query.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            for row in csv.DictReader(file):
                result[(partition, row["task_index"])] = row.get("scoring_points", "")
    return result


def _write_report(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _update_summary(path: Path, rows: list[dict[str, object]]) -> None:
    summary = OpenRcaBenchmarkSummary.model_validate_json(path.read_text(encoding="utf-8"))
    for strategy, metrics in summary.strategies.items():
        selected = [row for row in rows if row["strategy"] == strategy]
        count = len(selected) or 1
        per_partition = {}
        for partition in {str(row["partition"]) for row in selected}:
            partition_rows = [row for row in selected if row["partition"] == partition]
            partition_count = len(partition_rows)
            per_partition[partition] = {
                "strict_accuracy": sum(float(row["strict"]) for row in partition_rows)
                / partition_count,
                "partial_score": sum(
                    float(row["partial_score"]) for row in partition_rows
                )
                / partition_count,
            }
        summary.strategies[strategy] = metrics.model_copy(
            update={
                "strict_accuracy": sum(float(row["strict"]) for row in selected)
                / count,
                "partial_score": sum(
                    float(row["partial_score"]) for row in selected
                )
                / count,
                "component_score": sum(
                    float(row["component_score"]) for row in selected
                )
                / count,
                "reason_score": sum(float(row["reason_score"]) for row in selected)
                / count,
                "time_score": sum(float(row["time_score"]) for row in selected)
                / count,
                "per_partition": per_partition,
            }
        )
    path.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
