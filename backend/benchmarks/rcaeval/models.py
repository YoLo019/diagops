"""RCAEval RE2 准备与隔离的持久化契约模型。

这些模型是产物（manifest、pin、标签包）的唯一写入/读取契约，全部视为持久化
payload：`extra="forbid"`、有界字符串、拒绝 NaN/Infinity。runtime 侧契约
不得出现任何携带答案的字段（service/fault/repetition/source id 只属于标签包）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.multi_agent import DiagnosticStatus
from backend.domain.runtime import V11_RUN_DEADLINE_MAX_SECONDS

# 不透明 case ID 的形态：固定前缀 + 16 位十六进制，稳定且不携带源信息。
OPAQUE_CASE_ID_PATTERN = r"^re2-[0-9a-f]{16}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
# 源 case ID 与遥测文件名只允许单层安全标识符；含斜杠即天然排除路径穿越。
_SAFE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


def canonical_json_sha256(value: object) -> str:
    """canonical JSON（UTF-8、排序键、紧凑分隔符、拒绝非有限数）的 SHA256。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RcaEvalSystem(StrEnum):
    """RCAEval RE2 的三个微服务系统。"""

    ONLINE_BOUTIQUE = "online_boutique"
    SOCK_SHOP = "sock_shop"
    TRAIN_TICKET = "train_ticket"


class RcaEvalPartition(StrEnum):
    """本地留置分区：OB30 开发、SS15 封存验证、TT90 最终留置。"""

    OB30 = "ob30"
    SS15 = "ss15"
    TT90 = "tt90"


SYSTEM_TO_PARTITION: dict[RcaEvalSystem, RcaEvalPartition] = {
    RcaEvalSystem.ONLINE_BOUTIQUE: RcaEvalPartition.OB30,
    RcaEvalSystem.SOCK_SHOP: RcaEvalPartition.SS15,
    RcaEvalSystem.TRAIN_TICKET: RcaEvalPartition.TT90,
}

# 分区契约：OB30 每单元格 1 例共 30 例，SS15 选 15 个单元格各 1 例，
# TT90 每单元格 3 次重复共 90 例。
EXPECTED_PARTITION_COUNTS: dict[RcaEvalPartition, int] = {
    RcaEvalPartition.OB30: 30,
    RcaEvalPartition.SS15: 15,
    RcaEvalPartition.TT90: 90,
}


class SystemTaxonomy(BaseModel):
    """单个系统的标签全集：恰好 5 个服务 × 6 种故障，来自 custodian pin。"""

    model_config = ConfigDict(extra="forbid")

    services: list[str] = Field(min_length=5, max_length=5)
    faults: list[str] = Field(min_length=6, max_length=6)

    @field_validator("services", "faults")
    @classmethod
    def _unique_labels(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("taxonomy labels must be unique")
        return values


class SourcePin(BaseModel):
    """custodian 钉住的上游身份与源文件完整性清单。

    pin 存放在仓库之外（与数据集同侧），prepare 逐文件复核哈希后才允许重打包；
    任何被改动、缺失或多余的源文件都会 fail closed。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-source-pin-v1"] = "rcaeval-source-pin-v1"
    upstream_repository: str = Field(min_length=1, max_length=256)
    upstream_revision: str = Field(pattern=_REVISION_PATTERN)
    upstream_artifact: str = Field(min_length=1, max_length=128)
    archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_seed: str = Field(min_length=1, max_length=128)
    taxonomy: dict[RcaEvalSystem, SystemTaxonomy]
    files: dict[str, str]

    @field_validator("taxonomy")
    @classmethod
    def _all_systems(
        cls, value: dict[RcaEvalSystem, SystemTaxonomy]
    ) -> dict[RcaEvalSystem, SystemTaxonomy]:
        if set(value) != set(RcaEvalSystem):
            raise ValueError("taxonomy must cover exactly the three RE2 systems")
        return value

    @field_validator("files")
    @classmethod
    def _safe_relative_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path, digest in value.items():
            if (
                not path
                or path.startswith("/")
                or "\\" in path
                or ":" in path
                or ".." in path.split("/")
            ):
                raise ValueError(f"pin path traversal or unsafe path: {path!r}")
            if not path.startswith("cases/"):
                raise ValueError(f"pin path must live under cases/: {path!r}")
            if not re.fullmatch(_SHA256_PATTERN, digest):
                raise ValueError(f"pin file hash must be sha256 hex: {path!r}")
        return value


class SourceCaseDescriptor(BaseModel):
    """源归档中每个 case 目录下的 case.json（含答案标签，仅留在源侧与标签包）。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=_SAFE_NAME_PATTERN)
    system: RcaEvalSystem
    service: str = Field(min_length=1, max_length=128)
    fault: str = Field(min_length=1, max_length=128)
    repetition: int = Field(ge=1, le=3)
    files: list[str] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def _safe_file_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(_SAFE_NAME_PATTERN, name):
                raise ValueError(f"telemetry path traversal or unsafe name: {name!r}")
        if len(set(value)) != len(value):
            raise ValueError("telemetry file names must be unique within a case")
        return value


class RuntimeCaseEntry(BaseModel):
    """runtime 包中的单个 case：只有不透明 ID、分区与重命名后的遥测文件。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=OPAQUE_CASE_ID_PATTERN)
    partition: RcaEvalPartition
    files: list[str] = Field(min_length=1)


class RuntimeManifest(BaseModel):
    """runtime 包 manifest：非答案 provenance + 不透明 case 列表，零标签字段。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-runtime-manifest-v1"] = "rcaeval-runtime-manifest-v1"
    source_class: Literal["public_dataset"] = "public_dataset"
    upstream_repository: str = Field(min_length=1, max_length=256)
    upstream_revision: str = Field(pattern=_REVISION_PATTERN)
    upstream_artifact: str = Field(min_length=1, max_length=128)
    archive_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_seed: str = Field(min_length=1, max_length=128)
    partition_counts: dict[RcaEvalPartition, int] = Field(min_length=3, max_length=3)
    cases: list[RuntimeCaseEntry]
    manifest_hash: str = ""


class LabelEntry(BaseModel):
    """evaluator-only 标签：不透明 ID 与源标签的映射，绝不进入 runtime 包。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=OPAQUE_CASE_ID_PATTERN)
    partition: RcaEvalPartition
    source_case_id: str = Field(min_length=1, max_length=128)
    system: RcaEvalSystem
    service: str = Field(min_length=1, max_length=128)
    fault: str = Field(min_length=1, max_length=128)
    repetition: int = Field(ge=1, le=3)


class LabelManifest(BaseModel):
    """标签包 manifest；通过 runtime_manifest_hash 与 runtime 包成对绑定。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-label-manifest-v1"] = "rcaeval-label-manifest-v1"
    runtime_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    entries: list[LabelEntry]
    manifest_hash: str = ""


class RcaEvalConfiguration(StrEnum):
    """SS15/TT90 冻结配置；equal-token 只用于 SS15。"""

    SINGLE_INTENDED = "single_intended"
    MULTI_INTENDED = "multi_intended"
    SINGLE_EQUAL_TOKEN = "single_equal_token"
    MULTI_EQUAL_TOKEN = "multi_equal_token"

    @property
    def is_multi(self) -> bool:
        return self in {self.MULTI_INTENDED, self.MULTI_EQUAL_TOKEN}


# topology 由 configuration 机械派生：Single 一律 one-context (1,1)，
# Multi 一律 Lead+Investigators+Critic (3,2)；拍平、互换、篡改一律拒绝。
EXPECTED_CONFIGURATION_TOPOLOGY: dict[RcaEvalConfiguration, tuple[int, int]] = {
    RcaEvalConfiguration.SINGLE_INTENDED: (1, 1),
    RcaEvalConfiguration.SINGLE_EQUAL_TOKEN: (1, 1),
    RcaEvalConfiguration.MULTI_INTENDED: (3, 2),
    RcaEvalConfiguration.MULTI_EQUAL_TOKEN: (3, 2),
}


class EvaluationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration: RcaEvalConfiguration
    token_budget: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    tool_budget: int = Field(gt=0)
    # spec §9.1 默认 run deadline 120s；正式评测经逐次授权可放宽至
    # V11_RUN_DEADLINE_MAX_SECONDS（2026-08-15 运营噪声链，网关长生成/慢响应
    # 超出 120s 是运营噪声）。
    timeout_seconds: float = Field(
        gt=0, le=V11_RUN_DEADLINE_MAX_SECONDS, allow_inf_nan=False
    )
    max_investigators: int = Field(ge=1, le=3)
    max_rounds: int = Field(ge=1, le=2)


class EndpointCapabilityIdentity(BaseModel):
    """无凭据端点身份；正式 run 只接受已通过的 capability artifact。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=160)
    api_mode: Literal["responses", "chat_completions"]
    structured_output_transport: Literal[
        "native_json_schema", "strict_output_tool"
    ] = "native_json_schema"
    endpoint_id: str = Field(min_length=1, max_length=128)
    artifact_hash: str = Field(pattern=_SHA256_PATTERN)
    result: Literal["passed"] = "passed"


def materialize_ss15_configurations(
    *,
    single_budget: int,
    multi_budget: int,
    max_turns: int,
    tool_budget: int,
    timeout_seconds: float,
) -> dict[RcaEvalConfiguration, EvaluationBudget]:
    """物化四种公平配置；验证由 runner 的成组合同统一完成。"""
    common = {
        "max_turns": max_turns,
        "tool_budget": tool_budget,
        "timeout_seconds": timeout_seconds,
    }
    equal_budget = single_budget * 3
    return {
        RcaEvalConfiguration.SINGLE_INTENDED: EvaluationBudget(
            configuration=RcaEvalConfiguration.SINGLE_INTENDED,
            token_budget=single_budget,
            max_investigators=1,
            max_rounds=1,
            **common,
        ),
        RcaEvalConfiguration.MULTI_INTENDED: EvaluationBudget(
            configuration=RcaEvalConfiguration.MULTI_INTENDED,
            token_budget=multi_budget,
            max_investigators=3,
            max_rounds=2,
            **common,
        ),
        RcaEvalConfiguration.SINGLE_EQUAL_TOKEN: EvaluationBudget(
            configuration=RcaEvalConfiguration.SINGLE_EQUAL_TOKEN,
            token_budget=equal_budget,
            max_investigators=1,
            max_rounds=1,
            **common,
        ),
        RcaEvalConfiguration.MULTI_EQUAL_TOKEN: EvaluationBudget(
            configuration=RcaEvalConfiguration.MULTI_EQUAL_TOKEN,
            token_budget=equal_budget,
            max_investigators=3,
            max_rounds=2,
            **common,
        ),
    }


class CandidatePrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    affected_service: str = Field(min_length=1, max_length=128)
    failure_mechanism: str = Field(min_length=1, max_length=256)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    onset_window_start: datetime | None = None
    onset_window_end: datetime | None = None


class CandidateLifecycleAudit(BaseModel):
    """候选发布链路的安全摘要；不携带模型原始输出。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["candidate-lifecycle-v1"] = "candidate-lifecycle-v1"
    raw_structured_output_count: int = Field(default=0, ge=0)
    raw_structured_output_hashes: list[str] = Field(default_factory=list, max_length=16)
    parsed_draft_count: int = Field(default=0, ge=0)
    parsed_draft_hashes: list[str] = Field(default_factory=list, max_length=16)
    admitted_candidate_count: int = Field(default=0, ge=0)
    admitted_candidate_hashes: list[str] = Field(default_factory=list, max_length=16)
    persisted_candidate_count: int = Field(default=0, ge=0)
    persisted_candidate_hash: str | None = Field(default=None, max_length=64)
    authoritative_candidate_ids: list[str] = Field(default_factory=list, max_length=3)
    published_candidate_count: int = Field(default=0, ge=0)
    published_candidate_hash: str | None = Field(default=None, max_length=64)
    failure_categories: list[str] = Field(default_factory=list, max_length=16)
    drop_reasons: list[str] = Field(default_factory=list, max_length=16)


class CasePrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=OPAQUE_CASE_ID_PATTERN)
    configuration: RcaEvalConfiguration
    completed: bool
    diagnostic_status: DiagnosticStatus | None = None
    candidates: list[CandidatePrediction] = Field(default_factory=list, max_length=3)
    candidate_lifecycle: CandidateLifecycleAudit | None = None
    runtime_run_id: str = Field(min_length=1, max_length=128)
    execution_contract_hash: str = Field(pattern=_SHA256_PATTERN)
    available_evidence_ids: list[str] = Field(default_factory=list)
    evidence_summaries: dict[str, str] = Field(default_factory=dict)
    duration_ms: float = Field(default=0, ge=0, allow_inf_nan=False)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    read_only_violations: int = Field(default=0, ge=0)
    leakage_violations: int = Field(default=0, ge=0)
    failure_category: str | None = Field(default=None, max_length=128)
    # 案例级有界重试（best-of-2）：记录产出本预测的 attempt 数；
    # 每次 attempt 的完整 run 都在侧库中持久化可审计，bundle 语义不变。
    attempts: int = Field(default=1, ge=1, le=2)


class FrozenRunIdentity(BaseModel):
    """四配置/两配置之间必须逐字段相等的冻结身份。"""

    model_config = ConfigDict(extra="forbid")

    source_commit: str = Field(pattern=_REVISION_PATTERN)
    source_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    git_dirty: Literal[False] = False
    runtime_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    capability: EndpointCapabilityIdentity
    prompt_hash: str = Field(pattern=_SHA256_PATTERN)
    tool_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    skill_catalog_hash: str = Field(pattern=_SHA256_PATTERN)
    prediction_schema_hash: str = Field(pattern=_SHA256_PATTERN)
    normalizer_hash: str = Field(pattern=_SHA256_PATTERN)
    scorer_dependency_hash: str = Field(pattern=_SHA256_PATTERN)
    dependency_lock_hash: str = Field(pattern=_SHA256_PATTERN)
    memory_snapshot_hash: str = Field(pattern=_SHA256_PATTERN)
    retry_policy_hash: str = Field(pattern=_SHA256_PATTERN)


class PredictionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-predictions-v1"] = "rcaeval-predictions-v1"
    partition: RcaEvalPartition
    configuration: RcaEvalConfiguration
    identity: FrozenRunIdentity
    budget: EvaluationBudget
    predictions: list[CasePrediction]
    frozen_at: datetime
    bundle_hash: str = ""


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    exact_top1: float = Field(ge=0, le=1, allow_inf_nan=False)
    component_top1: float = Field(ge=0, le=1, allow_inf_nan=False)
    mechanism_top1: float = Field(ge=0, le=1, allow_inf_nan=False)
    top3: float = Field(ge=0, le=1, allow_inf_nan=False)
    time_window_accuracy: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    completion_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    reference_integrity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    p95_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tool_calls: int = Field(ge=0)
    read_only_violations: int = Field(ge=0)
    leakage_violations: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    failure_counts: dict[str, int] = Field(default_factory=dict)


class BootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    samples: int
    observed_delta: float = Field(allow_inf_nan=False)
    ci_low: float = Field(allow_inf_nan=False)
    ci_high: float = Field(allow_inf_nan=False)
    indices_hash: str = Field(pattern=_SHA256_PATTERN)


class McNemarResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discordant_single_only: int = Field(ge=0)
    discordant_multi_only: int = Field(ge=0)
    p_value: float = Field(ge=0, le=1, allow_inf_nan=False)


class PairedEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    single_configuration: RcaEvalConfiguration
    multi_configuration: RcaEvalConfiguration
    token_ratio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    bootstrap: BootstrapResult
    mcnemar: McNemarResult


class EvaluationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-evaluation-v1"] = "rcaeval-evaluation-v1"
    partition: RcaEvalPartition
    frozen_identity: FrozenRunIdentity
    runtime_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    labels_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    prediction_bundle_hashes: dict[RcaEvalConfiguration, str]
    summaries: dict[RcaEvalConfiguration, EvaluationSummary]
    paired: list[PairedEvaluation]
    label_open_count: Literal[1] = 1
    evaluated_at: datetime
    artifact_hash: str = ""


class AcceptancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-acceptance-policy-v1"] = (
        "rcaeval-acceptance-policy-v1"
    )
    sealed_validation_intended_budget_multi_agent_exact: float = Field(
        ge=0, le=1, allow_inf_nan=False
    )
    sealed_validation_artifact_hash: str = Field(pattern=_SHA256_PATTERN)
    final_exact_gate: float = Field(ge=0.6, le=1, allow_inf_nan=False)
    minimum_absolute_delta: Literal[0.1] = 0.1
    maximum_token_ratio: Literal[3.0] = 3.0
    minimum_reference_integrity: Literal[1.0] = 1.0
    minimum_evidence_support: Literal[0.95] = 0.95
    maximum_p95_latency_ms: Literal[120000.0] = 120000.0
    maximum_read_only_violations: Literal[0] = 0
    maximum_leakage_violations: Literal[0] = 0
    frozen_identity: FrozenRunIdentity
    tt90_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    evaluator_hash: str = Field(pattern=_SHA256_PATTERN)
    policy_hash: str = ""


class AcceptanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    gates: dict[str, bool]
    measured_single_exact: float = Field(ge=0, le=1, allow_inf_nan=False)
    measured_multi_exact: float = Field(ge=0, le=1, allow_inf_nan=False)
    measured_delta: float = Field(allow_inf_nan=False)
    token_ratio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    evidence_support: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class AcceptanceResultArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-acceptance-result-v1"] = (
        "rcaeval-acceptance-result-v1"
    )
    evaluation_artifact_hash: str = Field(pattern=_SHA256_PATTERN)
    policy_hash: str = Field(pattern=_SHA256_PATTERN)
    manual_audit_artifact_hash: str = Field(pattern=_SHA256_PATTERN)
    decision: AcceptanceDecision
    evaluated_at: datetime
    artifact_hash: str = ""


class EvidenceAuditPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: str = Field(pattern=_SHA256_PATTERN)
    case_id: str = Field(pattern=OPAQUE_CASE_ID_PATTERN)
    configuration: RcaEvalConfiguration
    candidate_rank: int = Field(ge=1, le=3)
    evidence_id: str = Field(min_length=1, max_length=128)
    affected_service: str = Field(min_length=1, max_length=128)
    failure_mechanism: str = Field(min_length=1, max_length=256)
    evidence_summary: str = Field(default="", max_length=512)
    onset_window_start: datetime | None = None
    onset_window_end: datetime | None = None
    onset_window_semantics: Literal["missing", "bounded_utc"]


class EvidenceAuditExport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-evidence-audit-export-v1"] = (
        "rcaeval-evidence-audit-export-v1"
    )
    prediction_bundle_hashes: dict[RcaEvalConfiguration, str] = Field(
        default_factory=dict
    )
    pairs: list[EvidenceAuditPair]
    export_hash: str = Field(pattern=_SHA256_PATTERN)


class EvidenceAuditDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: str = Field(pattern=_SHA256_PATTERN)
    entity_supported: bool
    temporally_compatible: bool
    mechanism_relevant: bool
    not_contradicted: bool
    reason_code: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=256)
    reviewer_id: str = Field(min_length=1, max_length=128)
    reviewed_at: datetime

    @property
    def passed(self) -> bool:
        return all(
            (
                self.entity_supported,
                self.temporally_compatible,
                self.mechanism_relevant,
                self.not_contradicted,
            )
        )


class ManualAuditArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rcaeval-manual-audit-v1"] = "rcaeval-manual-audit-v1"
    export_hash: str = Field(pattern=_SHA256_PATTERN)
    rubric_version: Literal["rcaeval-evidence-rubric-v1"] = "rcaeval-evidence-rubric-v1"
    rubric_hash: str = Field(pattern=_SHA256_PATTERN)
    reviewer_id: str = Field(min_length=1, max_length=128)
    decisions: list[EvidenceAuditDecision]
    pass_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    artifact_hash: str = ""
