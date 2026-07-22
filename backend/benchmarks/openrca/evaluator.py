from __future__ import annotations

import csv
import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.benchmarks.openrca.models import OpenRcaBenchmarkSummary, OpenRcaPartition

_PREDICTION_PATTERN = re.compile(
    r'{\s*'
    r'(?:(?:"root cause occurrence datetime"):\s*"(.*?)")?,?\s*'
    r'(?:(?:"root cause component"):\s*"(.*?)")?,?\s*'
    r'(?:(?:"root cause reason"):\s*"(.*?)")?\s*}'
)
_COMPONENT_PATTERN = re.compile(
    r"The (?:\d+-th|only) predicted root cause component is ([^\n]+)"
)
_REASON_PATTERN = re.compile(
    r"The (?:\d+-th|only) predicted root cause reason is ([^\n]+)"
)
_TIME_PATTERN = re.compile(
    r"The (?:\d+-th|only) root cause occurrence time is within 1 minutes "
    r"\(i\.e\., <=1min\) of ([^\n]+)"
)
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class EvaluationScore:
    strict: bool
    partial_score: float
    component_score: float
    reason_score: float
    time_score: float
    component_points: int = 0
    reason_points: int = 0
    time_points: int = 0


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

    component_points = sum(item.component is not None for item in expected)
    reason_points = sum(item.reason is not None for item in expected)
    time_points = sum(item.occurred_at is not None for item in expected)
    component_enabled = component_points == len(expected)
    reason_enabled = reason_points == len(expected)
    time_enabled = time_points == len(expected)
    denominator = component_points + reason_points + time_points
    if len(predicted) != len(expected) or denominator == 0:
        return EvaluationScore(False, 0.0, 0.0, 0.0, 0.0)

    best = (0, 0, 0)
    for ordering in itertools.permutations(predicted):
        component = reason = occurrence = 0
        for actual, target in zip(ordering, expected, strict=False):
            if component_enabled:
                component += int(actual.component == target.component)
            if reason_enabled:
                reason += int(actual.reason == target.reason)
            if time_enabled:
                occurrence += int(
                    actual.occurred_at is not None
                    and target.occurred_at is not None
                    and abs(actual.occurred_at - target.occurred_at)
                    <= timedelta(minutes=1)
                )
        best = max(best, (component, reason, occurrence), key=sum)

    total_matches = sum(best)
    partial = round(total_matches / denominator, 2)
    return EvaluationScore(
        strict=partial == 1.0,
        partial_score=partial,
        component_score=best[0] / component_points if component_points else 0.0,
        reason_score=best[1] / reason_points if reason_points else 0.0,
        time_score=best[2] / time_points if time_points else 0.0,
        component_points=component_points,
        reason_points=reason_points,
        time_points=time_points,
    )


class _ReplayLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    benchmark_run_id: str = Field(min_length=1, max_length=160)
    strategy: str = Field(pattern=r"^(fixed|adaptive)$")
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=160)
    case_id: str = Field(min_length=1, max_length=160)
    partition: OpenRcaPartition
    row_id: str = Field(pattern=r"^\d+$")
    prediction_root: str = Field(min_length=1, max_length=2048)
    prediction_file: str = Field(pattern=r"^(fixed|adaptive)-predictions\.csv$")
    prediction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_root: str = Field(min_length=1, max_length=2048)
    scoring_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def evaluate_persisted_prediction(runtime_store, runtime_run_id: str) -> str:
    """按持久 locator 与内容哈希重跑官方 prediction/scoring evaluator。"""
    raw_locator = runtime_store.get_benchmark_replay_locator(runtime_run_id)
    if raw_locator is None:
        raise FileNotFoundError("benchmark replay locator is missing")
    try:
        locator = _ReplayLocator.model_validate(raw_locator)
    except ValidationError as exc:
        raise ValueError("benchmark replay locator is invalid") from exc
    if locator.prediction_file != f"{locator.strategy}-predictions.csv":
        raise ValueError("benchmark prediction identity is invalid")
    run = runtime_store.get_run(runtime_run_id)
    if (
        run.model_provider is None
        or locator.provider != run.model_provider.value
        or locator.model != run.model_name
        or locator.prompt_version != run.prompt_version
    ):
        raise ValueError("benchmark runtime configuration is inconsistent")

    prediction_root = Path(locator.prediction_root).resolve()
    prediction_path = (prediction_root / locator.prediction_file).resolve()
    if (
        not prediction_path.is_relative_to(prediction_root)
        or not prediction_path.is_file()
    ):
        raise FileNotFoundError("benchmark prediction artifact is missing")
    prediction = _prediction_for_runtime(
        prediction_path, runtime_run_id, locator
    )
    if _sha256(prediction) != locator.prediction_sha256:
        raise ValueError("benchmark prediction artifact checksum mismatch")

    query_root = Path(locator.query_root).resolve()
    query_path = (query_root / locator.partition.value / "query.csv").resolve()
    if not query_path.is_relative_to(query_root) or not query_path.is_file():
        raise FileNotFoundError("benchmark scoring artifact is missing")
    with query_path.open(encoding="utf-8-sig", newline="") as file:
        query_rows = list(csv.DictReader(file))
    try:
        scoring_points = query_rows[int(locator.row_id)].get("scoring_points", "")
    except (IndexError, ValueError) as exc:
        raise ValueError("benchmark scoring row identity is invalid") from exc
    if _sha256(scoring_points) != locator.scoring_sha256:
        raise ValueError("benchmark scoring artifact checksum mismatch")

    score = evaluate_prediction(prediction, scoring_points)
    if score.strict:
        return "strict"
    if score.component_points + score.reason_points + score.time_points:
        return f"partial:{score.partial_score:.2f}"
    return "invalid"


def _prediction_for_runtime(
    path: Path, runtime_run_id: str, locator: _ReplayLocator
) -> str:
    matches = []
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            try:
                metadata = json.loads(row.get("metadata", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, dict)
                and metadata.get("runtime_run_id") == runtime_run_id
                and row.get("case_id") == locator.case_id
                and row.get("partition") == locator.partition.value
                and row.get("row_id") == locator.row_id
            ):
                matches.append(row.get("prediction", ""))
    if len(matches) != 1:
        raise ValueError("benchmark prediction row identity is invalid")
    return matches[0]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            key = (row["partition"], row["row_id"])
            score = evaluate_prediction(row["prediction"], scoring.get(key, ""))
            report_rows.append(
                {
                    "strategy": strategy,
                    "case_id": row["case_id"],
                    "partition": row["partition"],
                    "strict": int(score.strict),
                    "partial_score": score.partial_score,
                    "component_score": score.component_score,
                    "component_points": score.component_points,
                    "reason_score": score.reason_score,
                    "reason_points": score.reason_points,
                    "time_score": score.time_score,
                    "time_points": score.time_points,
                }
            )
    report_path = run_dir / "compatible-report.csv"
    _write_report(report_path, report_rows)
    _update_summary(run_dir / "summary.json", report_rows)
    _update_manifest_checksums(run_dir)
    return report_path


def write_official_query_inputs(
    query_root: Path, run_dir: Path, output_dir: Path
) -> list[Path]:
    """在冻结目录外生成与分区 prediction 等长、同序的官方 query 子集。"""
    resolved_run = run_dir.resolve()
    resolved_output = output_dir.resolve()
    if resolved_output == resolved_run or resolved_output.is_relative_to(resolved_run):
        raise ValueError("official query output must be outside the frozen run directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for partition in ("Bank", "Market/cloudbed-1", "Market/cloudbed-2", "Telecom"):
        slug = partition.replace("/", "-")
        prediction_path = run_dir / f"fixed-{slug}.csv"
        if not prediction_path.exists():
            prediction_path = run_dir / f"adaptive-{slug}.csv"
        with prediction_path.open(encoding="utf-8", newline="") as file:
            row_ids = sorted(int(row["row_id"]) for row in csv.DictReader(file))
        with (query_root / partition / "query.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)
            fieldnames = reader.fieldnames or []
            source_rows = list(reader)
        selected = [source_rows[row_id] for row_id in row_ids]
        target = output_dir / f"{slug}-query.csv"
        with target.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(selected)
        written.append(target)
    return written


def _parse_prediction(value: str) -> list[_Cause]:
    causes = []
    for occurred_at, component, reason in _PREDICTION_PATTERN.findall(value):
        try:
            parsed_time = datetime.strptime(occurred_at, _DATETIME_FORMAT)
        except ValueError:
            parsed_time = None
        causes.append(
            _Cause(
                parsed_time,
                component or None,
                reason or None,
            )
        )
    return causes


def _parse_scoring_points(value: str) -> list[_Cause]:
    components = _COMPONENT_PATTERN.findall(value)
    reasons = _REASON_PATTERN.findall(value)
    times = [
        datetime.strptime(item, _DATETIME_FORMAT)
        for item in _TIME_PATTERN.findall(value)
    ]
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
            for row_id, row in enumerate(csv.DictReader(file)):
                result[(partition, str(row_id))] = row.get("scoring_points", "")
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
                "component_score": _field_accuracy(selected, "component"),
                "reason_score": _field_accuracy(selected, "reason"),
                "time_score": _field_accuracy(selected, "time"),
                "per_partition": per_partition,
            }
        )
    path.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _field_accuracy(rows: list[dict[str, object]], field: str) -> float:
    points_key = f"{field}_points"
    score_key = f"{field}_score"
    points = sum(int(row[points_key]) for row in rows)
    if not points:
        return 0.0
    matches = sum(
        float(row[score_key]) * int(row[points_key])
        for row in rows
    )
    return matches / points


def _update_manifest_checksums(run_dir: Path) -> None:
    resolved_run = run_dir.resolve()
    manifest_path = run_dir / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("run manifest must be a JSON object")
    # 只记录冻结契约定义的文件，避免把意外落入 run 目录的本地文件名写入 manifest。
    fixed_names = {
        "fixed-predictions.csv",
        "adaptive-predictions.csv",
        "compatible-report.csv",
        "official-report.csv",
        "summary.json",
    }
    artifacts = [
        path
        for path in run_dir.iterdir()
        if path.is_file()
        and path.resolve().is_relative_to(resolved_run)
        and (
            path.name in fixed_names
            or (
                path.suffix == ".csv"
                and path.name.startswith(("fixed-", "adaptive-"))
            )
        )
    ]
    manifest["artifact_checksums"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(artifacts, key=lambda item: item.name)
    }
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
