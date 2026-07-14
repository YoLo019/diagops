from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from backend.domain.agent_plan import AgentExecution
from backend.domain.multi_agent import (
    ExecutionStepKind,
    FailureCategory,
    ResultValidationCategory,
    StabilizationCategory,
)
from backend.safety.redaction import (
    assert_safe_value,
    escape_markdown_code,
)

_CANONICAL_CASE_KEYS = frozenset(
    (case_id, repetition)
    for case_id in (
        "database_slowdown",
        "dependency_timeout",
        "deployment_regression",
        "single_bad_instance",
        "traffic_spike",
    )
    for repetition in range(1, 4)
)
_REQUIRED_CERTIFICATION_THRESHOLDS = frozenset(
    {
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
    }
)
_FAILURE_CLASSIFICATION_THRESHOLD = "failure_classification"


class ReliabilityExecutionStep(BaseModel):
    """可公开持久化的 Agent 执行步骤投影。"""

    model_config = ConfigDict(extra="forbid")

    step_kind: ExecutionStepKind
    agent_name: str
    analysis_round: int | None = None
    attempt: int = 1
    status: str
    failure_category: FailureCategory
    result_validation_category: ResultValidationCategory | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ReliabilityArtifact(BaseModel):
    """冻结的 schema-v3 可靠性工件合同。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    run_id: str
    mode: Literal["deterministic", "live", "diagnostic"]
    provider: str
    model: str
    started_at: datetime
    completed_at: datetime
    case_results: list[dict[str, JsonValue]] = Field(default_factory=list)
    aggregate: dict[str, JsonValue] = Field(default_factory=dict)


def project_execution(execution: AgentExecution) -> ReliabilityExecutionStep:
    """从执行记录中提取不含自由文本和传输元数据的 allowlist 字段。"""
    if execution.step_kind is None:
        raise ValueError("execution step kind required")
    return ReliabilityExecutionStep(
        step_kind=execution.step_kind,
        agent_name=execution.agent_name,
        analysis_round=execution.analysis_round,
        attempt=execution.attempt,
        status=execution.status.value,
        failure_category=execution.failure_category,
        result_validation_category=execution.result_validation_category,
        evidence_ids=list(execution.evidence_ids),
    )


def write_reliability_artifact(
    directory: Path,
    artifact: ReliabilityArtifact,
) -> Path:
    """通过同目录临时文件原子写入 schema-v3 JSON 和 Markdown 摘要。"""
    _validate_attribution(artifact)
    payload = artifact.model_dump(mode="json")
    assert_safe_value(payload)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / artifact.run_id / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)

    summary = path.with_name("summary.md")
    summary_temporary = summary.with_suffix(".tmp")
    summary_temporary.write_text(_markdown_summary(artifact), encoding="utf-8")
    summary_temporary.replace(summary)
    return path


def read_reliability_artifact(path: Path) -> ReliabilityArtifact | None:
    """读取 schema-v3；历史 v1/v2 保持只读并返回空结果。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if isinstance(version, int) and version > 3:
        raise ValueError("unsupported schema version")
    if version in {1, 2}:
        return None
    if version != 3:
        raise ValueError("unsupported schema version")
    return ReliabilityArtifact.model_validate(payload)


def latest_certification(
    directory: Path,
    provider: str,
    model: str,
) -> Literal["certified", "failed", "not_run"]:
    """返回指定 Provider/model 最新有效 live 工件的认证状态。"""
    matching: list[ReliabilityArtifact] = []
    if directory.exists():
        for path in directory.glob("*/result.json"):
            try:
                artifact = read_reliability_artifact(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                artifact is not None
                and artifact.mode == "live"
                and artifact.provider == provider
                and artifact.model == model
                and _is_canonical_certification_artifact(artifact)
            ):
                matching.append(artifact)
    if not matching:
        return "not_run"
    latest = max(matching, key=lambda item: (item.completed_at, item.run_id))
    return "certified" if latest.aggregate.get("passed") is True else "failed"


def has_safe_primary_classification(
    *,
    real_review: bool,
    run_status: str | None,
    primary_category: str | None,
) -> bool:
    """要求非 real-review 或 partial 行提供非 unknown 的安全主分类。"""
    requires_classification = not real_review or run_status == "partial"
    return not requires_classification or primary_category not in {
        None,
        "",
        StabilizationCategory.UNKNOWN.value,
    }


def _is_canonical_certification_artifact(artifact: ReliabilityArtifact) -> bool:
    """只让完整且自洽的 canonical live 工件参与 Provider 认证。"""
    if len(artifact.case_results) != len(_CANONICAL_CASE_KEYS):
        return False
    observed: set[tuple[str, int]] = set()
    classifications_valid = True
    for row in artifact.case_results:
        case_id = row.get("case_id")
        repetition = row.get("repetition")
        real_review = row.get("real_review")
        run_status = row.get("run_status")
        primary_category = row.get("primary_stabilization_category")
        if (
            not isinstance(case_id, str)
            or type(repetition) is not int
            or type(real_review) is not bool
            or (run_status is not None and not isinstance(run_status, str))
            or (
                primary_category is not None
                and not isinstance(primary_category, str)
            )
        ):
            return False
        key = (case_id, repetition)
        if (
            key not in _CANONICAL_CASE_KEYS
            or key in observed
            or row.get("provider") != artifact.provider
            or row.get("model") != artifact.model
        ):
            return False
        observed.add(key)
        classifications_valid &= has_safe_primary_classification(
            real_review=real_review,
            run_status=run_status,
            primary_category=primary_category,
        )
    if observed != _CANONICAL_CASE_KEYS:
        return False

    thresholds = artifact.aggregate.get("thresholds")
    if not isinstance(thresholds, dict):
        return False
    threshold_results: list[bool] = []
    for name in _REQUIRED_CERTIFICATION_THRESHOLDS:
        detail = thresholds.get(name)
        if not isinstance(detail, dict) or type(detail.get("passed")) is not bool:
            return False
        threshold_results.append(detail["passed"])
    classification_detail = thresholds.get(_FAILURE_CLASSIFICATION_THRESHOLD)
    if classification_detail is None:
        # 早期 schema-v3 工件没有独立 threshold，只在所有行可推导时兼容读取。
        if not classifications_valid:
            return False
    elif (
        not isinstance(classification_detail, dict)
        or type(classification_detail.get("passed")) is not bool
        or classification_detail["passed"] != classifications_valid
    ):
        return False
    else:
        threshold_results.append(classification_detail["passed"])
    aggregate_passed = artifact.aggregate.get("passed")
    return type(aggregate_passed) is bool and aggregate_passed == all(threshold_results)


def _validate_attribution(artifact: ReliabilityArtifact) -> None:
    provider = artifact.provider.strip()
    model = artifact.model.strip()
    if not provider or not model:
        raise ValueError("provider and model required")
    if artifact.mode == "deterministic" and provider != "substitute":
        raise ValueError("deterministic provider must be substitute")


def _markdown_summary(artifact: ReliabilityArtifact) -> str:
    passed = artifact.aggregate.get("passed")
    gate = "passed" if passed is True else "failed" if passed is False else "not_run"
    return "\n".join(
        [
            "# Reliability Run",
            "",
            f"- Run: `{escape_markdown_code(artifact.run_id)}`",
            f"- Mode: `{escape_markdown_code(artifact.mode)}`",
            f"- Provider: `{escape_markdown_code(artifact.provider)}`",
            f"- Model: `{escape_markdown_code(artifact.model)}`",
            f"- Started: `{escape_markdown_code(artifact.started_at.isoformat())}`",
            f"- Completed: `{escape_markdown_code(artifact.completed_at.isoformat())}`",
            f"- Gate: `{gate}`",
            "",
        ]
    )
