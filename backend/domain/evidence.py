from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)
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
    TRACE = "trace"
    RUNTIME_STATE = "runtime_state"
    VERIFIED_INCIDENT = "verified_incident"


class EvidenceKind(StrEnum):
    LOG_PATTERN = "log_pattern"
    METRIC_TREND = "metric_trend"
    DEPLOYMENT = "deployment"
    DEPENDENCY_HEALTH = "dependency_health"
    SERVICE_METADATA = "service_metadata"
    RELATED_ALERT = "related_alert"
    PROVIDER_ERROR = "provider_error"
    TRACE_PATH = "trace_path"
    TRACE_ERROR = "trace_error"
    TRACE_LATENCY = "trace_latency"
    RUNTIME_STATE = "runtime_state"
    VERIFIED_INCIDENT = "verified_incident"


class EvidenceStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceSourceClass(StrEnum):
    PUBLIC_DATASET = "public_dataset"
    RECORDED_LOCAL = "recorded_local"
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    LIVE_BACKEND = "live_backend"


class EvidenceProvenance(BaseModel):
    source_class: EvidenceSourceClass
    provider_profile: str = Field(min_length=1, max_length=128)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    source_artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    adapter_version: str = Field(min_length=1, max_length=64)


class EvidenceScope(BaseModel):
    entity_ids: list[str] = Field(default_factory=list, max_length=20)
    observed_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    signal_type: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_scope_window(self) -> EvidenceScope:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("scope window_start and window_end must appear together")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_start > self.window_end
        ):
            raise ValueError("scope window_start must not be later than window_end")
        return self


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
    scope: EvidenceScope | None = None
    provenance: EvidenceProvenance | None = None
    runtime_run_id: str | None = None

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in ("scope", "provenance", "runtime_run_id"):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data

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
