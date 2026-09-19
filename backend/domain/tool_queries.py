from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.domain.evidence import (
    AlertStatus,
    CanonicalTraceId,
    RuntimeStateValue,
    Severity,
)

LogKeyword = Annotated[str, Field(min_length=1, max_length=120)]
LogLevel = Annotated[str, Field(min_length=1, max_length=32)]
MetricName = Annotated[str, Field(min_length=1, max_length=160)]
# V11 遥测 scope 统一使用同一安全标识符边界（spec 8.1）。
SafeEntityId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")
]


class MetricAggregation(StrEnum):
    AVG = "avg"
    MAX = "max"
    SUM = "sum"


class PrometheusMetric(StrEnum):
    QPS = "qps"
    ERROR_RATE_5XX = "5xx_rate"
    P95_LATENCY = "p95_latency"
    CPU = "cpu"
    MEMORY = "memory"
    NETWORK_DROPS = "network_drops"
    PROCESS_RESTARTS = "process_restarts"


class DependencyDirection(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


class QueryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    start_time: datetime
    end_time: datetime
    reason: str = Field(min_length=1, max_length=240)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_window(self) -> "QueryWindow":
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("query timestamps must be timezone-aware")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.end_time - self.start_time > timedelta(hours=2):
            raise ValueError("query window cannot exceed two hours")
        return self


class LogQuery(QueryWindow):
    keywords: list[LogKeyword] = Field(
        default_factory=list, max_length=8,
        description="All keywords must match the same record (AND, case-insensitive). "
        "Use one discriminating term or [] to inspect coverage; do not list alternatives.",
    )
    levels: list[LogLevel] = Field(default_factory=list, max_length=5)
    instance: str | None = Field(default=None, max_length=160)


class MetricQuery(QueryWindow):
    metric_names: list[MetricName] = Field(default_factory=list, max_length=20)
    aggregation: MetricAggregation = MetricAggregation.AVG
    instance: str | None = Field(default=None, max_length=160)


class PrometheusQuery(QueryWindow):
    # 上限与 PrometheusMetric 基数同步：单次调用可覆盖全部受支持信号。
    metric_names: list[PrometheusMetric] = Field(min_length=1, max_length=7)
    aggregation: MetricAggregation = MetricAggregation.SUM
    instance: str | None = Field(default=None, max_length=160)


class DeploymentQuery(QueryWindow):
    version: str | None = Field(default=None, max_length=120)
    instance: str | None = Field(default=None, max_length=160)


class ServiceCatalogQuery(QueryWindow):
    include_dependencies: bool = True
    # 可选服务名过滤；缺省返回全部目录条目。
    name: str | None = Field(default=None, min_length=1, max_length=128)


class DependencyQuery(QueryWindow):
    direction: DependencyDirection = DependencyDirection.DOWNSTREAM
    target: str | None = Field(default=None, max_length=160)
    depth: Literal[1] = 1


class TraceDirection(StrEnum):
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"
    BOTH = "both"


_UniqueItemT = TypeVar("_UniqueItemT", bound=StrEnum)


def _ensure_unique(values: list[_UniqueItemT], field_name: str) -> list[_UniqueItemT]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class ScopedTelemetryQuery(BaseModel):
    """V11 新遥测查询的共享有界 scope；window 缺省时由 Provider 落到事件窗口。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_ids: list[SafeEntityId] = Field(default_factory=list, max_length=20)
    window_start: datetime | None = None
    window_end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_window(self) -> "ScopedTelemetryQuery":
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end must appear together")
        if self.window_start is not None and self.window_end is not None:
            if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
                raise ValueError("query timestamps must be timezone-aware")
            if self.window_end < self.window_start:
                raise ValueError("window_end must not be earlier than window_start")
        return self


class TraceQuery(ScopedTelemetryQuery):
    service: SafeEntityId | None = None
    operation: SafeEntityId | None = None
    trace_id: CanonicalTraceId | None = None
    error_only: bool = False
    min_duration_ms: float | None = Field(
        default=None, ge=0, le=3_600_000, allow_inf_nan=False
    )
    direction: TraceDirection = TraceDirection.BOTH


class RuntimeStateQuery(ScopedTelemetryQuery):
    states: list[RuntimeStateValue] = Field(default_factory=list, max_length=8)
    include_healthy: bool = False

    @field_validator("states")
    @classmethod
    def validate_unique_states(
        cls, values: list[RuntimeStateValue]
    ) -> list[RuntimeStateValue]:
        return _ensure_unique(values, "states")


class RelatedAlertQuery(ScopedTelemetryQuery):
    severities: list[Severity] = Field(default_factory=list, max_length=3)
    statuses: list[AlertStatus] = Field(default_factory=list, max_length=2)

    @field_validator("severities", "statuses")
    @classmethod
    def validate_unique_filters(
        cls, values: list[_UniqueItemT], info
    ) -> list[_UniqueItemT]:
        return _ensure_unique(values, info.field_name)


class MemoryQuery(BaseModel):
    """service/environment 由当前 investigation 固定，不作为查询字段暴露。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    affected_entity: str | None = Field(default=None, min_length=1, max_length=128)
    failure_mechanism: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=5, ge=1, le=10)
