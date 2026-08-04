"""RCAEval RE2 准备与隔离的持久化契约模型。

这些模型是产物（manifest、pin、标签包）的唯一写入/读取契约，全部视为持久化
payload：`extra="forbid"`、有界字符串、拒绝 NaN/Infinity。runtime 侧契约
不得出现任何携带答案的字段（service/fault/repetition/source id 只属于标签包）。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 不透明 case ID 的形态：固定前缀 + 16 位十六进制，稳定且不携带源信息。
OPAQUE_CASE_ID_PATTERN = r"^re2-[0-9a-f]{16}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
# 源 case ID 与遥测文件名只允许单层安全标识符；含斜杠即天然排除路径穿越。
_SAFE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class RcaEvalSystem(StrEnum):
    """RCAEval RE2 的三个微服务系统。"""

    ONLINE_BOUTIQUE = "online_boutique"
    SOCK_SHOP = "sock_shop"
    TRAIN_TICKET = "train_ticket"


class RcaEvalPartition(StrEnum):
    """本地留置分区：OB30 开发、SS30 封存验证、TT90 最终留置。"""

    OB30 = "ob30"
    SS30 = "ss30"
    TT90 = "tt90"


SYSTEM_TO_PARTITION: dict[RcaEvalSystem, RcaEvalPartition] = {
    RcaEvalSystem.ONLINE_BOUTIQUE: RcaEvalPartition.OB30,
    RcaEvalSystem.SOCK_SHOP: RcaEvalPartition.SS30,
    RcaEvalSystem.TRAIN_TICKET: RcaEvalPartition.TT90,
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
