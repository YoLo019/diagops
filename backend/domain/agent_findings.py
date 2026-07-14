from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ModelProvider,
    MultiAgentRunStatus,
    StabilizationCategory,
)


class AgentName(StrEnum):
    LOG = "LogAgent"
    METRIC = "MetricAgent"
    DEPLOYMENT = "DeploymentAgent"


class AgentFindingType(StrEnum):
    SIGNAL = "signal"
    ROOT_CAUSE = "root_cause"
    CONTRADICTION = "contradiction"
    GAP = "gap"


class AgentFindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentFinding(BaseModel):
    id: str = Field(default_factory=lambda: f"finding-{uuid4().hex}")
    investigation_id: str
    agent_name: AgentName
    finding_type: AgentFindingType
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] = 1
    revises_finding_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_finding(self) -> "AgentFinding":
        if self.finding_type != AgentFindingType.GAP and not self.evidence_ids:
            raise ValueError("evidence_ids required unless finding_type is gap")
        if self.analysis_round == 1 and self.revises_finding_id is not None:
            raise ValueError("round 1 cannot set revises_finding_id")
        if self.analysis_round == 2 and self.revises_finding_id is None:
            raise ValueError("round 2 requires revises_finding_id")
        return self


class RootCauseCandidate(BaseModel):
    id: str = Field(default_factory=lambda: f"candidate-{uuid4().hex}")
    cause_type: CauseType
    summary: str = Field(min_length=1)
    rank: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    supporting_finding_ids: list[str] = Field(default_factory=list)
    contradicting_finding_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    uncertainty: str = ""


class CoordinationReview(BaseModel):
    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    id: str = Field(default_factory=lambda: f"coordination-{uuid4().hex}")
    investigation_id: str
    candidates: list[RootCauseCandidate] = Field(default_factory=list)
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    run_status: MultiAgentRunStatus = MultiAgentRunStatus.COMPLETED
    decision_status: CoordinationDecisionStatus | None = None
    baseline_cause_type: CauseType | None = None
    selected_cause_type: CauseType | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    primary_stabilization_category: StabilizationCategory | None = None
    secondary_stabilization_categories: list[StabilizationCategory] = Field(
        default_factory=list
    )
    summary: str = ""
    uncertainty: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def sort_candidates(self) -> "CoordinationReview":
        self.candidates.sort(key=lambda candidate: candidate.rank)
        return self


class WorkbenchGraphNode(BaseModel):
    id: str
    label: str
    type: str


class WorkbenchGraphEdge(BaseModel):
    source: str
    target: str
    relation: str


class WorkbenchGraphSeed(BaseModel):
    nodes: list[WorkbenchGraphNode] = Field(default_factory=list)
    edges: list[WorkbenchGraphEdge] = Field(default_factory=list)
