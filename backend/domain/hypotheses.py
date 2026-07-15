from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class CauseType(StrEnum):
    DEPLOYMENT_REGRESSION = "deployment_regression"
    TRAFFIC_SPIKE = "traffic_spike"
    DOWNSTREAM_DEPENDENCY_FAILURE = "downstream_dependency_failure"
    DATABASE_SLOWDOWN = "database_slowdown"
    SINGLE_INSTANCE_ISSUE = "single_instance_issue"
    RESOURCE_SATURATION = "resource_saturation"
    NETWORK_FAULT = "network_fault"
    CONFIGURATION_ERROR = "configuration_error"
    PROCESS_OR_CONTAINER_FAILURE = "process_or_container_failure"
    INFRASTRUCTURE_FAULT = "infrastructure_fault"
    UNKNOWN = "unknown"


class Hypothesis(BaseModel):
    id: str = Field(default_factory=lambda: f"hyp-{uuid4().hex}")
    cause_type: CauseType
    summary: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
