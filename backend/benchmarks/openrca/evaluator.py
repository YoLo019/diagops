from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaPairedComparison,
    OpenRcaPairedSide,
    OpenRcaPartition,
    OpenRcaRuntimeIndex,
)

_PREDICTION_PATTERN = re.compile(
    r"{\s*"
    r'(?:(?:"root cause occurrence datetime"):\s*"(.*?)")?,?\s*'
    r'(?:(?:"root cause component"):\s*"(.*?)")?,?\s*'
    r'(?:(?:"root cause reason"):\s*"(.*?)")?\s*}'
)
_COMPONENT_PATTERN = re.compile(r"The (?:\d+-th|only) predicted root cause component is ([^\n]+)")
_REASON_PATTERN = re.compile(r"The (?:\d+-th|only) predicted root cause reason is ([^\n]+)")
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
                    and abs(actual.occurred_at - target.occurred_at) <= timedelta(minutes=1)
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
    run = runtime_store.get_run(runtime_run_id)
    if run.model_provider is None and run.model_name == "deterministic":
        return "not_recorded"
    raw_locator = runtime_store.get_benchmark_replay_locator(runtime_run_id)
    if raw_locator is None:
        raise FileNotFoundError("benchmark replay locator is missing")
    try:
        locator = _ReplayLocator.model_validate(raw_locator)
    except ValidationError as exc:
        raise ValueError("benchmark replay locator is invalid") from exc
    if locator.prediction_file != f"{locator.strategy}-predictions.csv":
        raise ValueError("benchmark prediction identity is invalid")
    if (
        run.model_provider is None
        or locator.provider != run.model_provider.value
        or locator.model != run.model_name
        or locator.prompt_version != run.prompt_version
    ):
        raise ValueError("benchmark runtime configuration is inconsistent")

    prediction_root = Path(locator.prediction_root).resolve()
    prediction_path = (prediction_root / locator.prediction_file).resolve()
    if not prediction_path.is_relative_to(prediction_root) or not prediction_path.is_file():
        raise FileNotFoundError("benchmark prediction artifact is missing")
    prediction = _prediction_for_runtime(prediction_path, runtime_run_id, locator)
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


def _prediction_for_runtime(path: Path, runtime_run_id: str, locator: _ReplayLocator) -> str:
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
        with prediction_path.open(encoding="utf-8", newline="") as file:
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


def gate_run(run_dir: Path) -> list[str]:
    """只读取冻结产物，检查 deterministic Fixed 发布门禁。"""
    failures: list[str] = []
    try:
        manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
        summary = OpenRcaBenchmarkSummary.model_validate_json(
            (run_dir / "summary.json").read_text(encoding="utf-8")
        )
        fixed = summary.strategies["fixed"]
    except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
        return [f"frozen benchmark artifact is invalid: {type(exc).__name__}"]
    if not isinstance(manifest, dict):
        return ["frozen benchmark artifact is invalid: manifest must be an object"]

    if manifest.get("mode") != "deterministic":
        failures.append("mode must be deterministic")
    if manifest.get("strategies") != ["fixed"] or set(summary.strategies) != {"fixed"}:
        failures.append("release gate only accepts the Fixed strategy")
    checksums = manifest.get("artifact_checksums", {})
    if not isinstance(checksums, dict) or not {
        "fixed-predictions.csv",
        "summary.json",
    }.issubset(checksums):
        failures.append("frozen artifact checksums are incomplete")
    elif any(
        not isinstance(name, str)
        or Path(name).name != name
        or not isinstance(expected, str)
        or not (run_dir / name).is_file()
        or hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != expected
        for name, expected in checksums.items()
    ):
        failures.append("frozen artifact checksum mismatch")
    if summary.case_count != 40 or fixed.case_count != 40:
        failures.append("Fixed release gate requires exactly 40 cases")
    if fixed.completed_count != 40:
        failures.append("Fixed release gate requires 40 completed Runtime Runs")
    if summary.model != "deterministic" or summary.prompt_version != "v10-shared-core":
        failures.append("deterministic model identity is invalid")
    if fixed.strict_accuracy is None:
        failures.append("Fixed strict accuracy must be recorded")

    try:
        with (run_dir / "official-report.csv").open(encoding="utf-8-sig", newline="") as file:
            official_rows = list(csv.DictReader(file))
        scores = [float(row["score"]) for row in official_rows]
        if (
            len(scores) != fixed.case_count
            or any(not math.isfinite(score) for score in scores)
            or sum(scores) / len(scores) < 0.10
        ):
            failures.append("official Fixed partial score must be at least 0.10")
    except (FileNotFoundError, KeyError, ValueError, ZeroDivisionError):
        failures.append("official Fixed partial score is unavailable")

    for score_field in ("component_score", "reason_score", "time_score"):
        value = getattr(fixed, score_field)
        if value is None or not math.isfinite(value) or value <= 0:
            failures.append(f"Fixed {score_field.removesuffix('_score')} score must be positive")

    try:
        with (run_dir / "fixed-predictions.csv").open(encoding="utf-8", newline="") as file:
            predictions = list(csv.DictReader(file))
    except FileNotFoundError:
        predictions = []
    cardinality_matches = nonempty = 0
    for row in predictions:
        try:
            metadata = json.loads(row["metadata"])
            expected = metadata["expected_root_cause_count"]
            prediction = json.loads(row["prediction"])
            valid_prediction = isinstance(prediction, dict)
            cardinality_matches += int(
                type(expected) is int
                and expected in (1, 2)
                and valid_prediction
                and len(prediction) == expected
            )
            nonempty += int(valid_prediction and bool(prediction))
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    prediction_count = len(predictions)
    if prediction_count != 40 or cardinality_matches != prediction_count:
        failures.append("prediction cardinality must match metadata for 100% of cases")
    if not prediction_count or nonempty / prediction_count < 0.95:
        failures.append("non-empty predictions must cover at least 95% of cases")

    if fixed.evidence_reference_validity != 1 or fixed.invalid_evidence_references != 0:
        failures.append("evidence reference validity must be 100%")
    if fixed.read_only_violations:
        failures.append("read-only violations must be zero")
    if fixed.estimated_cost != 0:
        failures.append("deterministic estimated cost must be zero")
    if fixed.input_tokens or fixed.output_tokens:
        failures.append("deterministic tokens must be zero")
    return failures


def targeted_gate_run(run_dir: Path) -> list[str]:
    """检查六案例开发回归，不替代正式 40-case release Gate。"""
    failures: list[str] = []
    try:
        summary = OpenRcaBenchmarkSummary.model_validate_json(
            (run_dir / "summary.json").read_text(encoding="utf-8")
        )
        fixed = summary.strategies["fixed"]
        with (run_dir / "fixed-predictions.csv").open(
            encoding="utf-8",
            newline="",
        ) as file:
            predictions = list(csv.DictReader(file))
        with (run_dir / "official-report.csv").open(
            encoding="utf-8-sig",
            newline="",
        ) as file:
            official = list(csv.DictReader(file))
    except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
        return [f"targeted artifact is invalid: {type(exc).__name__}"]

    if summary.case_count != 6 or fixed.case_count != 6:
        failures.append("targeted gate requires exactly 6 cases")
    if fixed.completed_count != 6:
        failures.append("targeted gate requires 6 completed Runtime Runs")
    if (
        fixed.evidence_reference_validity != 1
        or fixed.invalid_evidence_references != 0
    ):
        failures.append("targeted evidence reference validity must be 100%")
    if fixed.read_only_violations:
        failures.append("targeted read-only violations must be zero")
    if fixed.projection_errors:
        failures.append("targeted projection errors must be zero")
    if fixed.projection_fallbacks:
        failures.append("targeted projection fallbacks must be zero")

    parsed = []
    for row in official:
        try:
            score = float(row["score"])
            task_index = row["task_index"]
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(score):
            parsed.append((task_index, score))
    if len(parsed) != 6 or sum(score > 0 for _, score in parsed) < 3:
        failures.append("official targeted score must pass at least 3 of 6 cases")
    for task_index in ("task_1", "task_2", "task_3"):
        if not any(task == task_index and score > 0 for task, score in parsed):
            failures.append(f"official targeted {task_index} must have a positive score")

    valid_projection_rows = 0
    for row in predictions:
        try:
            projection = json.loads(row["metadata"])["projection"]
            valid_projection_rows += int(
                projection["rule_version"] == "v2"
                and projection["projection_error"] is False
                and projection["projection_fallback"] is False
                and bool(projection["selected_evidence_ids"])
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    if len(predictions) != 6 or valid_projection_rows != 6:
        failures.append("all targeted predictions require valid projection audit metadata")
    return failures


# 预注册的官方 evaluator 冻结提交。它是审计身份而不是可调参数：任何变更都必须
# 伴随新的 spec 批准，不提供命令行或配置覆盖。
APPROVED_OFFICIAL_EVALUATOR_COMMIT = "c1bd4af7f635171a1c31cdd567c07d698dff6abc"


@dataclass
class _PairedSide:
    label: str
    failures: list[str] = field(default_factory=list)
    run_id: str = ""
    source_sha: str = ""
    case_manifest_hash: str = ""
    pre_eval_manifest_sha256: str = ""
    official_partial: Decimal | None = None
    strict: float | None = None
    component: float | None = None
    reason: float | None = None
    time: float | None = None
    case_count: int = 0
    completed_count: int = 0
    evidence_validity: float | None = None
    invalid_evidence: int = 0
    projection_errors: int = 0
    projection_fallbacks: int = 0
    read_only: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    execution_artifacts: dict[str, str] = field(default_factory=dict)
    evaluation_artifacts: dict[str, str] = field(default_factory=dict)


def paired_gate_run(
    *,
    safe_index: Path,
    baseline_run_dir: Path,
    baseline_evaluation_dir: Path,
    candidate_run_dir: Path,
    candidate_evaluation_dir: Path,
    official_evaluator_root: Path,
    output_path: Path,
) -> list[str]:
    """一次性揭盲 paired Gate：只读校验两个冻结 bundle 与其 evaluation copies。

    除输出位置非法外，无论通过与否都把 comparison 落盘；失败原因原样返回并写入
    artifact，绝不只保留通过结果。
    """
    resolved_output = output_path.resolve()
    for directory in (
        baseline_run_dir,
        baseline_evaluation_dir,
        candidate_run_dir,
        candidate_evaluation_dir,
    ):
        resolved_dir = directory.resolve()
        if resolved_output == resolved_dir or resolved_output.is_relative_to(resolved_dir):
            return ["paired gate output must be outside execution and evaluation directories"]

    try:
        index = OpenRcaRuntimeIndex.model_validate_json(safe_index.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"paired safe-index is invalid: {type(exc).__name__}"]
    safe_index_sha = hashlib.sha256(safe_index.read_bytes()).hexdigest()

    evaluator_head, failures = _official_evaluator_identity(official_evaluator_root)

    sides: dict[str, _PairedSide] = {}
    for label, run_dir, evaluation_dir in (
        ("baseline", baseline_run_dir, baseline_evaluation_dir),
        ("candidate", candidate_run_dir, candidate_evaluation_dir),
    ):
        try:
            side = _load_paired_side(label, run_dir, evaluation_dir, index)
        except (OSError, KeyError, ValueError, ValidationError) as exc:
            return failures + [f"{label} paired artifact is invalid: {type(exc).__name__}"]
        failures.extend(side.failures)
        sides[label] = side
    baseline = sides["baseline"]
    candidate = sides["candidate"]

    for item in gate_run(candidate_evaluation_dir):
        failures.append(f"candidate single-run gate: {item}")
    if candidate.projection_errors or candidate.projection_fallbacks:
        failures.append("candidate projection errors and fallbacks must be zero")

    if candidate.strict is None or baseline.strict is None:
        failures.append("compatible strict accuracy must be recorded for both sides")
    elif candidate.strict < baseline.strict:
        failures.append("candidate compatible strict accuracy must not regress below baseline")
    compatible_deltas: dict[str, Decimal | None] = {}
    for name in ("component", "reason", "time"):
        candidate_value = getattr(candidate, name)
        baseline_value = getattr(baseline, name)
        if candidate_value is None or baseline_value is None:
            failures.append(f"compatible {name} score must be recorded for both sides")
            compatible_deltas[name] = None
            continue
        compatible_deltas[name] = Decimal(str(candidate_value)) - Decimal(str(baseline_value))
        if candidate_value <= 0 or candidate_value < baseline_value:
            failures.append(
                f"candidate compatible {name} score must be positive "
                "and not regress below baseline"
            )

    official_delta: Decimal | None = None
    if candidate.official_partial is not None and baseline.official_partial is not None:
        # 只用 40 个原始有限 score 的 Decimal 均值比较，不使用任何显示或四舍五入值。
        official_delta = candidate.official_partial - baseline.official_partial
        if official_delta < Decimal("0.05"):
            failures.append(
                "candidate official partial score must improve by at least 0.05 over baseline"
            )

    strict_delta = (
        None
        if candidate.strict is None or baseline.strict is None
        else Decimal(str(candidate.strict)) - Decimal(str(baseline.strict))
    )
    comparison = OpenRcaPairedComparison(
        safe_index_sha256=safe_index_sha,
        official_evaluator_commit=evaluator_head,
        baseline=_paired_side_model(baseline),
        candidate=_paired_side_model(candidate),
        official_partial_delta=_decimal_text(official_delta),
        compatible_strict_delta=_decimal_text(strict_delta),
        compatible_component_delta=_decimal_text(compatible_deltas["component"]),
        compatible_reason_delta=_decimal_text(compatible_deltas["reason"]),
        compatible_time_delta=_decimal_text(compatible_deltas["time"]),
        passed=not failures,
        failures=failures,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(comparison.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return failures


def _official_evaluator_identity(root: Path) -> tuple[str, list[str]]:
    """返回官方 evaluator 实际 HEAD 与身份失败；实际 HEAD 总是写入 comparison。"""
    if not root.is_dir():
        return "", ["official evaluator root is missing"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if head.returncode != 0 or status.returncode != 0:
        return "", ["official evaluator root must be a readable git repository"]
    actual = head.stdout.strip()
    failures = []
    if actual != APPROVED_OFFICIAL_EVALUATOR_COMMIT:
        failures.append("official evaluator HEAD must be the approved commit")
    if status.stdout.strip():
        failures.append("official evaluator worktree must be clean")
    return actual, failures


def _load_paired_side(
    label: str,
    run_dir: Path,
    evaluation_dir: Path,
    index: OpenRcaRuntimeIndex,
) -> _PairedSide:
    side = _PairedSide(label)
    execution_manifest_bytes = (run_dir / "run-manifest.json").read_bytes()
    execution_manifest = json.loads(execution_manifest_bytes)
    evaluation_manifest = json.loads(
        (evaluation_dir / "run-manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(execution_manifest, dict) or not isinstance(evaluation_manifest, dict):
        raise ValueError(f"{label} run manifest must be a JSON object")
    # pre-eval 身份只取 execution bundle 的 manifest；evaluation copy 只允许
    # evaluate_run 更新 artifact_checksums，移除该字段后两边 canonical JSON 必须一致。
    side.pre_eval_manifest_sha256 = hashlib.sha256(execution_manifest_bytes).hexdigest()
    if _canonical_manifest(execution_manifest) != _canonical_manifest(evaluation_manifest):
        side.failures.append(
            f"{label} evaluation manifest must match the execution manifest "
            "except artifact_checksums"
        )

    execution_predictions = (run_dir / "fixed-predictions.csv").read_bytes()
    evaluation_predictions = (evaluation_dir / "fixed-predictions.csv").read_bytes()
    if execution_predictions != evaluation_predictions:
        side.failures.append(
            f"{label} evaluation predictions must be byte-for-byte identical "
            "to the execution bundle"
        )
    prediction_rows = list(csv.DictReader(io.StringIO(execution_predictions.decode("utf-8"))))
    case_ids = [row.get("case_id", "") for row in prediction_rows]
    expected_ids = {case.case_id for case in index.cases}
    predictions_by_case: dict[str, str] = {}
    case_set_ok = True
    if len(case_ids) != len(set(case_ids)) or set(case_ids) != expected_ids:
        side.failures.append(
            f"{label} prediction case set must contain each safe-index case exactly once"
        )
        case_set_ok = False
    else:
        predictions_by_case = {row["case_id"]: row["prediction"] for row in prediction_rows}

    with (evaluation_dir / "official-report.csv").open(encoding="utf-8-sig", newline="") as file:
        report_rows = list(csv.DictReader(file))
    # 官方 CSV 没有 case ID，行数必须先独立锁定，否则 multiset 错配无法定位。
    if len(report_rows) != 40:
        side.failures.append(f"{label} official report must contain exactly 40 rows")
    else:
        scores = []
        scores_finite = True
        for row in report_rows:
            try:
                score = Decimal(row["score"])
            except (KeyError, InvalidOperation):
                scores_finite = False
                break
            if not score.is_finite():
                scores_finite = False
                break
            scores.append(score)
        if not scores_finite:
            side.failures.append(f"{label} official scores must be finite")
        else:
            side.official_partial = sum(scores) / len(scores)
        if case_set_ok:
            expected_pairs = Counter(
                (
                    _canonical_query(case.instruction),
                    _canonical_answer(predictions_by_case[case.case_id]),
                )
                for case in index.cases
            )
            try:
                actual_pairs = Counter(
                    (_canonical_query(row["query"]), _canonical_answer(row["answer"]))
                    for row in report_rows
                )
            except (KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"{label} official report (query, answer) is invalid") from exc
            if actual_pairs != expected_pairs:
                side.failures.append(
                    f"{label} official report (query, answer) multiset must match "
                    "the frozen predictions"
                )

    summary = OpenRcaBenchmarkSummary.model_validate_json(
        (evaluation_dir / "summary.json").read_text(encoding="utf-8")
    )
    fixed = summary.strategies["fixed"]
    side.run_id = str(execution_manifest.get("run_id") or summary.run_id)
    side.source_sha = str(execution_manifest.get("git_commit", ""))
    side.case_manifest_hash = str(execution_manifest.get("case_manifest_hash", ""))
    side.strict = fixed.strict_accuracy
    side.component = fixed.component_score
    side.reason = fixed.reason_score
    side.time = fixed.time_score
    side.case_count = fixed.case_count
    side.completed_count = fixed.completed_count
    side.evidence_validity = fixed.evidence_reference_validity
    side.invalid_evidence = fixed.invalid_evidence_references
    side.projection_errors = fixed.projection_errors
    side.projection_fallbacks = fixed.projection_fallbacks
    side.read_only = fixed.read_only_violations
    side.input_tokens = fixed.input_tokens
    side.output_tokens = fixed.output_tokens
    side.cost = fixed.estimated_cost
    side.execution_artifacts = _artifact_shas(run_dir)
    side.evaluation_artifacts = _artifact_shas(evaluation_dir)
    return side


def _canonical_manifest(manifest: dict) -> str:
    trimmed = {key: value for key, value in manifest.items() if key != "artifact_checksums"}
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_query(value: str) -> str:
    # 合同只允许 CRLF -> LF 一种规范化，不做 strip 或大小写处理。
    return value.replace("\r\n", "\n")


def _canonical_answer(value: str) -> str:
    parsed = json.loads(value)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _artifact_shas(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file()
    }


def _decimal_text(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(Decimal(str(value)))


def _paired_side_model(side: _PairedSide) -> OpenRcaPairedSide:
    return OpenRcaPairedSide(
        run_id=side.run_id or "unknown",
        source_sha=side.source_sha,
        case_manifest_hash=side.case_manifest_hash,
        pre_eval_manifest_sha256=side.pre_eval_manifest_sha256,
        official_partial_score=_decimal_text(side.official_partial),
        compatible_strict_accuracy=_decimal_text(side.strict),
        compatible_component_score=_decimal_text(side.component),
        compatible_reason_score=_decimal_text(side.reason),
        compatible_time_score=_decimal_text(side.time),
        case_count=side.case_count,
        completed_count=side.completed_count,
        evidence_reference_validity=_decimal_text(side.evidence_validity),
        invalid_evidence_references=side.invalid_evidence,
        projection_errors=side.projection_errors,
        projection_fallbacks=side.projection_fallbacks,
        read_only_violations=side.read_only,
        input_tokens=side.input_tokens,
        output_tokens=side.output_tokens,
        estimated_cost=_decimal_text(side.cost),
        execution_artifact_sha256=side.execution_artifacts,
        evaluation_artifact_sha256=side.evaluation_artifacts,
    )


def write_official_query_inputs(query_root: Path, run_dir: Path, output_dir: Path) -> list[Path]:
    """在冻结目录外生成与分区 prediction 等长、同序的官方 query 子集。"""
    resolved_run = run_dir.resolve()
    resolved_output = output_dir.resolve()
    if resolved_output == resolved_run or resolved_output.is_relative_to(resolved_run):
        raise ValueError("official query output must be outside the frozen run directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for partition in ("Bank", "Market/cloudbed-1", "Market/cloudbed-2", "Telecom"):
        slug = partition.replace("/", "-")
        prediction_path = next(
            (
                candidate
                for candidate in (
                    run_dir / f"fixed-{slug}.csv",
                    run_dir / f"adaptive-{slug}.csv",
                )
                if candidate.exists()
            ),
            None,
        )
        if prediction_path is None:
            continue
        with prediction_path.open(encoding="utf-8", newline="") as file:
            row_ids = sorted(int(row["row_id"]) for row in csv.DictReader(file))
        with (query_root / partition / "query.csv").open(encoding="utf-8-sig", newline="") as file:
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
        with (query_root / partition / "query.csv").open(encoding="utf-8-sig", newline="") as file:
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
                "partial_score": sum(float(row["partial_score"]) for row in partition_rows)
                / partition_count,
            }
        summary.strategies[strategy] = metrics.model_copy(
            update={
                "strict_accuracy": sum(float(row["strict"]) for row in selected) / count,
                "partial_score": sum(float(row["partial_score"]) for row in selected) / count,
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
    matches = sum(float(row[score_key]) * int(row[points_key]) for row in rows)
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
            or (path.suffix == ".csv" and path.name.startswith(("fixed-", "adaptive-")))
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
