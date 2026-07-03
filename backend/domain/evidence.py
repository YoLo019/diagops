from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field
from typing_extensions import TypeAliasType

JsonPrimitive = str | int | float | bool | None
JsonValue = TypeAliasType("JsonValue", JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"])


class EvidenceProvider(StrEnum):
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    DEPENDENCY = "dependency"
    SERVICE_CATALOG = "service_catalog"


class EvidenceKind(StrEnum):
    LOG_PATTERN = "log_pattern"
    METRIC_TREND = "metric_trend"
    DEPLOYMENT = "deployment"
    DEPENDENCY_HEALTH = "dependency_health"
    SERVICE_METADATA = "service_metadata"


class EvidenceItem(BaseModel):
    id: str = Field(default_factory=lambda: f"ev-{uuid4().hex}")
    provider: EvidenceProvider
    kind: EvidenceKind
    timestamp: datetime
    summary: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
