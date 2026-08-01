from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


class OpenRcaPartition(StrEnum):
    BANK = "Bank"
    TELECOM = "Telecom"
    MARKET_CLOUDBED_1 = "Market/cloudbed-1"
    MARKET_CLOUDBED_2 = "Market/cloudbed-2"


class OpenRcaDifficulty(StrEnum):
    EASY = "easy"
    MIDDLE = "middle"
    HARD = "hard"


class OpenRcaFailureMode(StrEnum):
    SINGLE = "single"
    MULTI = "multi"


class OpenRcaManifestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    partition: OpenRcaPartition
    row_id: str = Field(pattern=r"^\d+$")
    task_index: str = Field(pattern=r"^task_[1-7]$")
    difficulty: OpenRcaDifficulty
    failure_mode: OpenRcaFailureMode
    telemetry_dir: str


class OpenRcaManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    seed: int
    per_partition: int = Field(ge=1)
    cases: list[OpenRcaManifestCase]
    manifest_hash: str = ""


class OpenRcaRuntimeCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    partition: OpenRcaPartition
    row_id: str = Field(pattern=r"^\d+$")
    task_index: str = Field(pattern=r"^task_[1-7]$")
    system: str = "openrca"
    date: str = ""
    service: str = "openrca-system"
    instruction: str
    expected_root_cause_count: int | None = Field(default=None, ge=1, le=2)
    timezone: str = "Asia/Shanghai"
    start_time: datetime
    end_time: datetime
    telemetry_dir: str


class OpenRcaRuntimeIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_manifest_hash: str
    cases: list[OpenRcaRuntimeCase]


class OpenRcaStrategySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    evidence_reference_validity: float = Field(ge=0, le=1)
    invalid_evidence_references: int = Field(ge=0)
    read_only_violations: int = Field(ge=0)
    average_tool_calls: float = Field(ge=0)
    duplicate_query_rejections: int = Field(ge=0)
    average_duration_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)
    strict_accuracy: float | None = Field(default=None, ge=0, le=1)
    partial_score: float | None = Field(default=None, ge=0, le=1)
    component_score: float | None = Field(default=None, ge=0, le=1)
    reason_score: float | None = Field(default=None, ge=0, le=1)
    time_score: float | None = Field(default=None, ge=0, le=1)
    per_partition: dict[str, dict[str, float]] = Field(default_factory=dict)
    failed_cases: list[dict[str, str]] = Field(default_factory=list)
    projection_errors: int = Field(default=0, ge=0)
    projection_fallbacks: int = Field(default=0, ge=0)


class OpenRcaBenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    case_count: int = Field(ge=0)
    model: str
    prompt_version: str
    git_commit: str
    started_at: datetime
    completed_at: datetime
    strategies: dict[str, OpenRcaStrategySummary]


def _require_finite_decimal(value: str) -> str:
    # 合同要求拒绝 NaN/Infinity；InvalidOperation 不是 ValueError，必须转成
    # ValidationError 而不是让原始算术异常逃出校验层。
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("decimal metric must be parseable") from exc
    if not parsed.is_finite():
        raise ValueError("decimal metric must be finite")
    return value


# Decimal 指标只以规范字符串持久化，避免 float 舍入进入审计 artifact。
DecimalString = Annotated[str, AfterValidator(_require_finite_decimal)]


class OpenRcaPairedSide(BaseModel):
    """paired Gate 单侧（baseline 或 candidate）的冻结身份与指标证据。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    source_sha: str
    case_manifest_hash: str
    pre_eval_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_partial_score: DecimalString | None = None
    compatible_strict_accuracy: DecimalString | None = None
    compatible_component_score: DecimalString | None = None
    compatible_reason_score: DecimalString | None = None
    compatible_time_score: DecimalString | None = None
    case_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    evidence_reference_validity: DecimalString | None = None
    invalid_evidence_references: int = Field(ge=0)
    projection_errors: int = Field(ge=0)
    projection_fallbacks: int = Field(ge=0)
    read_only_violations: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: DecimalString | None = None
    execution_artifact_sha256: dict[str, str] = Field(default_factory=dict)
    evaluation_artifact_sha256: dict[str, str] = Field(default_factory=dict)


class OpenRcaPairedComparison(BaseModel):
    """一次性揭盲 paired Gate 的唯一比较 artifact；失败结果同样必须完整落盘。"""

    model_config = ConfigDict(extra="forbid")

    safe_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_evaluator_commit: str
    baseline: OpenRcaPairedSide
    candidate: OpenRcaPairedSide
    official_partial_delta: DecimalString | None = None
    compatible_strict_delta: DecimalString | None = None
    compatible_component_delta: DecimalString | None = None
    compatible_reason_delta: DecimalString | None = None
    compatible_time_delta: DecimalString | None = None
    passed: bool
    failures: list[str] = Field(default_factory=list)
