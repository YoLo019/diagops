from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LogKeyword = Annotated[str, Field(min_length=1, max_length=120)]
LogLevel = Annotated[str, Field(min_length=1, max_length=32)]
MetricName = Annotated[str, Field(min_length=1, max_length=160)]


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
    keywords: list[LogKeyword] = Field(default_factory=list, max_length=8)
    levels: list[LogLevel] = Field(default_factory=list, max_length=5)
    instance: str | None = Field(default=None, max_length=160)


class MetricQuery(QueryWindow):
    metric_names: list[MetricName] = Field(default_factory=list, max_length=20)
    aggregation: MetricAggregation = MetricAggregation.AVG
    instance: str | None = Field(default=None, max_length=160)


class PrometheusQuery(QueryWindow):
    metric_names: list[PrometheusMetric] = Field(min_length=1, max_length=5)
    aggregation: MetricAggregation = MetricAggregation.SUM
    instance: str | None = Field(default=None, max_length=160)


class DeploymentQuery(QueryWindow):
    version: str | None = Field(default=None, max_length=120)
    instance: str | None = Field(default=None, max_length=160)


class ServiceCatalogQuery(QueryWindow):
    include_dependencies: bool = True


class DependencyQuery(QueryWindow):
    direction: DependencyDirection = DependencyDirection.DOWNSTREAM
    target: str | None = Field(default=None, max_length=160)
    depth: Literal[1] = 1
