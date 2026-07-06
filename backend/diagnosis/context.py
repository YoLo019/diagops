from enum import StrEnum

from pydantic import BaseModel, Field

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.providers.results import ProviderResult, ProviderStatus


class SpecialistStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class SpecialistResult(BaseModel):
    agent_name: str
    status: SpecialistStatus
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    summary: str
    errors: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)


class DiagnosisContext(BaseModel):
    event: IncidentEvent
    evidence: list[EvidenceItem] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    specialist_results: list[SpecialistResult] = Field(default_factory=list)


def provider_status_to_specialist_status(status: ProviderStatus) -> SpecialistStatus:
    if status == ProviderStatus.SUCCESS:
        return SpecialistStatus.COMPLETED
    if status == ProviderStatus.PARTIAL:
        return SpecialistStatus.PARTIAL
    if status == ProviderStatus.SKIPPED:
        return SpecialistStatus.SKIPPED
    return SpecialistStatus.FAILED
