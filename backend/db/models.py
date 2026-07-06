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
from backend.domain.reports import IncidentReport
from backend.providers.results import ProviderResult


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvestigationRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"inv-{uuid4().hex}")
    event: IncidentEvent
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
