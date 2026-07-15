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
    task_index: str
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
    instruction: str
    timezone: str = "Asia/Shanghai"
    start_time: datetime
    end_time: datetime
    telemetry_dir: str


class OpenRcaRuntimeIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_manifest_hash: str
    cases: list[OpenRcaRuntimeCase]
