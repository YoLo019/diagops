from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer, model_validator

from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.agent_findings import CoordinationReview, CriticAssessment
from backend.domain.agent_plan import LeadDecision
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
    lead_decision: LeadDecision | None = None
    critic_assessments: list[CriticAssessment] = Field(default_factory=list)
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
                self.lead_decision is not None,
                bool(self.critic_assessments),
                self.active_runtime_run_id is not None,
            )
        )
        if not has_v11_projection:
            for field_name in (
                "top_affected_entity",
                "top_failure_mechanism",
                "diagnostic_status",
                "authority_mode",
                "lead_decision",
                "critic_assessments",
                "active_runtime_run_id",
            ):
                data.pop(field_name, None)
        if "runtime_available" not in self.model_fields_set:
            data.pop("runtime_available", None)
        return data

    @classmethod
    def from_record(
        cls,
        record: InvestigationRecord,
        review: CoordinationReview | None = None,
    ) -> "InvestigationSummary":
        """从完整调查记录生成不含详情集合的安全列表投影。"""
        top = record.hypotheses[0] if record.hypotheses else None
        authoritative_candidate = None
        if review is not None and review.authority_mode == AuthorityMode.AGENT:
            candidate_ids = set(review.authoritative_candidate_ids)
            authoritative_candidate = next(
                (
                    candidate
                    for candidate in review.candidates
                    if candidate.id in candidate_ids
                ),
                None,
            )
        v11_review = (
            review
            if review is not None and review.authority_mode == AuthorityMode.AGENT
            else None
        )
        v11_report = (
            record.report
            if record.report is not None
            and record.report.authority_mode == AuthorityMode.AGENT
            else None
        )
        report_candidate = v11_report.diagnoses[0] if v11_report and v11_report.diagnoses else None
        v11_projection = bool(
            v11_review is not None
            or v11_report is not None
            or record.active_runtime_run_id is not None
            or (
                record.multi_agent_run is not None
                and record.multi_agent_run.authority_mode == AuthorityMode.AGENT
            )
        )
        return cls(
            id=record.id,
            status=record.status.value,
            service=redact_text(record.event.service),
            title=redact_text(record.event.title),
            strategy=record.strategy,
            top_cause_type=(
                "unknown"
                if v11_projection
                else top.cause_type.value if top else "unknown"
            ),
            confidence=(
                authoritative_candidate.confidence
                if v11_review is not None and authoritative_candidate is not None
                else report_candidate.confidence
                if report_candidate is not None
                else 0.0
                if v11_projection
                else top.confidence if top else 0.0
            ),
            top_affected_entity=(
                authoritative_candidate.affected_entity
                if v11_review is not None and authoritative_candidate is not None
                else report_candidate.affected_entity
                if report_candidate is not None
                else None
                if v11_projection
                else getattr(top, "affected_entity", None) if top is not None else None
            ),
            top_failure_mechanism=(
                authoritative_candidate.failure_mechanism
                if v11_review is not None and authoritative_candidate is not None
                else report_candidate.failure_mechanism
                if report_candidate is not None
                else None
                if v11_projection
                else getattr(top, "failure_mechanism", None) if top is not None else None
            ),
            diagnostic_status=(
                v11_review.diagnostic_status
                if v11_review is not None
                else v11_report.diagnostic_status
                if v11_report is not None
                else record.multi_agent_run.diagnostic_status
                if record.multi_agent_run is not None
                else None
            ),
            authority_mode=(
                v11_review.authority_mode
                if v11_review is not None
                else v11_report.authority_mode
                if v11_report is not None
                else record.multi_agent_run.authority_mode
                if record.multi_agent_run is not None
                else None
            ),
            lead_decision=v11_review.lead_decision if v11_review is not None else None,
            critic_assessments=(
                list(v11_review.critic_assessments) if v11_review is not None else []
            ),
            active_runtime_run_id=record.active_runtime_run_id,
            action_count=len(record.actions),
            verification_count=len(record.verification_suggestions),
            failure_reason=(
                redact_text(record.failure_reason) if record.failure_reason else None
            ),
        )
