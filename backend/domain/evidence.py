from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from typing_extensions import TypeAliasType

JsonPrimitive = str | int | float | bool | None
JsonValue = TypeAliasType("JsonValue", JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"])


class EvidenceProvider(StrEnum):
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    DEPENDENCY = "dependency"
    SERVICE_CATALOG = "service_catalog"
    RELATED_ALERT = "related_alert"


class EvidenceKind(StrEnum):
    LOG_PATTERN = "log_pattern"
    METRIC_TREND = "metric_trend"
    DEPLOYMENT = "deployment"
    DEPENDENCY_HEALTH = "dependency_health"
    SERVICE_METADATA = "service_metadata"
    RELATED_ALERT = "related_alert"
    PROVIDER_ERROR = "provider_error"


class EvidenceStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceItem(BaseModel):
    id: str = Field(default_factory=lambda: f"ev-{uuid4().hex}")
    provider: EvidenceProvider
    kind: EvidenceKind
    timestamp: datetime
    summary: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    status: EvidenceStatus = EvidenceStatus.SUCCESS
    error_message: str | None = None

    @field_validator("payload")
    @classmethod
    def reject_non_finite_payload_floats(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        def validate_json_value(value: JsonValue) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("payload must not contain non-finite float values")
            if isinstance(value, list):
                for item in value:
                    validate_json_value(item)
            if isinstance(value, dict):
                for item in value.values():
                    validate_json_value(item)

        validate_json_value(payload)
        return payload
