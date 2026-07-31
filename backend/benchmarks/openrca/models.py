from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
