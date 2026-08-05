from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer, model_validator

from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.llm_analysis import LLMAnalysis
from backend.domain.multi_agent import (
    AuthorityMode,
    DiagnosticStatus,
    InvestigationStrategy,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.providers.results import ProviderResult
from backend.safety.redaction import redact_text


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvestigationRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"inv-{uuid4().hex}")
    event: IncidentEvent
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    multi_agent_run: MultiAgentRunSummary | None = None
    status: InvestigationStatus = InvestigationStatus.PENDING
    evidence: list[EvidenceItem] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    specialist_results: list[SpecialistResult] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    report: IncidentReport | None = None
    llm_analysis: LLMAnalysis | None = None
    failure_reason: str | None = None
    actions: list[RecommendedAction] = Field(default_factory=list)
    verification_suggestions: list[VerificationSuggestion] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    runtime_available: bool = False
    active_runtime_run_id: str | None = None
    source_investigation_id: str | None = None

    @model_validator(mode="after")
    def validate_runtime_ownership(self) -> "InvestigationRecord":
        owners = {
            item.runtime_run_id
            for item in self.evidence
            if item.runtime_run_id is not None
        }
        if self.report is not None and self.report.runtime_run_id is not None:
            owners.add(self.report.runtime_run_id)
        owners.update(
            item.runtime_run_id
            for item in self.actions + self.verification_suggestions
            if item.runtime_run_id is not None
        )
        if self.multi_agent_run is not None and self.multi_agent_run.runtime_run_id:
            owners.add(self.multi_agent_run.runtime_run_id)
        if self.active_runtime_run_id is not None:
            owners.add(self.active_runtime_run_id)
        if len(owners) > 1:
            raise ValueError("investigation aggregate mixes runtime owners")
        if (
            self.multi_agent_run is not None
            and self.multi_agent_run.authority_mode == AuthorityMode.AGENT
            and self.multi_agent_run.runtime_run_id is None
        ):
            raise ValueError("V11 summary requires runtime_run_id")
        return self

    @model_serializer(mode="wrap")
    def serialize_runtime_projection(self, handler):
        data = handler(self)
        if "runtime_available" not in self.model_fields_set:
            data.pop("runtime_available", None)
        for field_name in ("active_runtime_run_id", "source_investigation_id"):
            if field_name not in self.model_fields_set:
                data.pop(field_name, None)
        return data


class InvestigationSummary(BaseModel):
    id: str
    status: str
    service: str
    title: str
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    top_cause_type: str
    confidence: float
    top_affected_entity: str | None = None
    top_failure_mechanism: str | None = None
    diagnostic_status: DiagnosticStatus | None = None
    authority_mode: AuthorityMode | None = None
    active_runtime_run_id: str | None = None
    action_count: int = 0
    verification_count: int = 0
    failure_reason: str | None = None
    runtime_available: bool = False

    @model_serializer(mode="wrap")
    def serialize_runtime_projection(self, handler):
        data = handler(self)
        has_v11_projection = any(
            (
                self.top_affected_entity is not None,
                self.top_failure_mechanism is not None,
                self.diagnostic_status is not None,
                self.authority_mode is not None,
                self.active_runtime_run_id is not None,
            )
        )
        if not has_v11_projection:
            for field_name in (
                "top_affected_entity",
                "top_failure_mechanism",
                "diagnostic_status",
                "authority_mode",
                "active_runtime_run_id",
            ):
                data.pop(field_name, None)
        if "runtime_available" not in self.model_fields_set:
            data.pop("runtime_available", None)
        return data

    @classmethod
    def from_record(cls, record: InvestigationRecord) -> "InvestigationSummary":
        """从完整调查记录生成不含详情集合的安全列表投影。"""
        top = record.hypotheses[0] if record.hypotheses else None
        return cls(
            id=record.id,
            status=record.status.value,
            service=redact_text(record.event.service),
            title=redact_text(record.event.title),
            strategy=record.strategy,
            top_cause_type=top.cause_type.value if top else "unknown",
            confidence=top.confidence if top else 0.0,
            top_affected_entity=(
                getattr(top, "affected_entity", None) if top is not None else None
            ),
            top_failure_mechanism=(
                getattr(top, "failure_mechanism", None) if top is not None else None
            ),
            diagnostic_status=(
                record.multi_agent_run.diagnostic_status
                if record.multi_agent_run is not None
                else None
            ),
            authority_mode=(
                record.multi_agent_run.authority_mode
                if record.multi_agent_run is not None
                else None
            ),
            active_runtime_run_id=record.active_runtime_run_id,
            action_count=len(record.actions),
            verification_count=len(record.verification_suggestions),
            failure_reason=(
                redact_text(record.failure_reason) if record.failure_reason else None
            ),
        )
