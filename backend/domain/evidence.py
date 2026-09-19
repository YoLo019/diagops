from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import TypeAliasType

from backend.domain.events import Severity

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


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


class RuntimeKind(StrEnum):
    CONTAINER = "container"
    POD = "pod"
    NODE = "node"
    PROCESS = "process"
    UNKNOWN = "unknown"


class RuntimeStateValue(StrEnum):
    UNKNOWN = "unknown"
    RESTARTING = "restarting"
    CRASH_LOOP = "crash_loop"
    OOM_KILLED = "oom_killed"
    PENDING = "pending"
    NOT_READY = "not_ready"
    TERMINATED = "terminated"
    NODE_PRESSURE = "node_pressure"
    HEALTHY = "healthy"


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


# span/trace 属性与告警 label 共用同一组安全边界，避免第二类自由文本通道。
AlertLabelKey = Annotated[str, Field(min_length=1, max_length=64)]
AlertLabelValue = Annotated[str, Field(max_length=256)]
CanonicalTraceId = Annotated[str, Field(pattern=r"^([0-9a-f]{16}|[0-9a-f]{32})$")]
CanonicalSpanId = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]


class TraceChildTiming(BaseModel):
    """同服务直接子 span 的区间并集；未覆盖部分包含未埋点等待，不是 CPU 自耗时。"""

    model_config = ConfigDict(extra="forbid")

    observed_child_count: int = Field(ge=1)
    covered_ms: float = Field(ge=0, allow_inf_nan=False)
    uncovered_ms: float = Field(ge=0, allow_inf_nan=False)
    longest_child_span_id: CanonicalSpanId
    longest_child_operation: str = Field(max_length=128)
    longest_child_duration_ms: float = Field(ge=0, allow_inf_nan=False)
    longest_child_peer_service: str | None = Field(default=None, max_length=128)
    longest_child_peer_duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class TraceSpanPayload(BaseModel):
    """进入 EvidenceItem.payload 前的唯一 span 投影；原始后端 JSON 不直接落库。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: CanonicalTraceId
    span_id: CanonicalSpanId
    parent_span_id: CanonicalSpanId | None = None
    service: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    started_at: datetime
    duration_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    status: SpanStatus = SpanStatus.UNSET
    attributes: dict[AlertLabelKey, AlertLabelValue] = Field(default_factory=dict, max_length=20)
    child_timing: TraceChildTiming | None = None

    @model_validator(mode="after")
    def normalize_grpc_method(self):
        # gRPC 的全限定方法名不是文件路径；去掉协议允许的前导斜杠后再统一脱敏。
        if self.attributes.get("rpc.system") == "grpc" and re.fullmatch(
            r"/(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*/[A-Za-z_]\w*", self.operation,
        ):
            self.operation = self.operation[1:]
        return self


class RuntimeStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=128)
    runtime_kind: RuntimeKind = RuntimeKind.UNKNOWN
    state: RuntimeStateValue
    reason: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    restart_count: int | None = Field(default=None, ge=0)
    ready: bool | None = None


class RelatedAlertPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=128)
    severity: Severity
    status: AlertStatus
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[AlertLabelKey, AlertLabelValue] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_alert_window(self) -> RelatedAlertPayload:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("alert ends_at must not be earlier than starts_at")
        return self


class VerifiedIncidentPayload(BaseModel):
    """verified memory 证据只携带来源身份与有界摘要，不复用外部证据 ID。"""

    model_config = ConfigDict(extra="forbid")

    source_investigation_id: str = Field(min_length=1, max_length=128)
    root_candidate_id: str = Field(min_length=1, max_length=128)
    verified_at: datetime
    summary: str = Field(min_length=1, max_length=512)
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=512)


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


def validate_usable_evidence(
    evidence: Iterable[EvidenceItem],
    evidence_ids: Iterable[str],
    *,
    runtime_run_id: str | None = None,
) -> None:
    """校验被诊断结论引用的证据可用且属于同一 runtime owner。"""
    evidence_by_id = {item.id: item for item in evidence}
    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            raise ValueError(f"missing evidence id: {evidence_id}")
        if item.status not in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}:
            raise ValueError(
                f"evidence {evidence_id} has unusable status {item.status.value}; "
                "usable evidence requires success or partial status"
            )
        if runtime_run_id is not None and item.runtime_run_id != runtime_run_id:
            raise ValueError(f"evidence {evidence_id} owner mismatch")


def validate_committed_evidence(
    evidence: Iterable[EvidenceItem],
    evidence_ids: Iterable[str],
    *,
    runtime_run_id: str | None = None,
) -> None:
    """校验引用指向同 run 已提交证据，不限状态（GAP finding 的引用口径）。

    GAP 的语义是"证据缺失"，引用采集 failed/skipped 的已提交记录正是其正确
    出处（spec §7.2/§8.2）；此函数只做存在性与 owner 校验，不做 usable 要求。
    """
    evidence_by_id = {item.id: item for item in evidence}
    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            raise ValueError(f"missing evidence id: {evidence_id}")
        if runtime_run_id is not None and item.runtime_run_id != runtime_run_id:
            raise ValueError(f"evidence {evidence_id} owner mismatch")
