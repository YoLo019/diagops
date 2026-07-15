from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.llm_analysis import LLMAnalysis
from backend.domain.multi_agent import InvestigationStrategy
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


class InvestigationSummary(BaseModel):
    id: str
    status: str
    service: str
    title: str
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    top_cause_type: str
    confidence: float
    action_count: int = 0
    verification_count: int = 0
    failure_reason: str | None = None

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
            action_count=len(record.actions),
            verification_count=len(record.verification_suggestions),
            failure_reason=(
                redact_text(record.failure_reason) if record.failure_reason else None
            ),
        )
