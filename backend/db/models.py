from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.reports import IncidentReport


class InvestigationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class InvestigationRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"inv-{uuid4().hex}")
    event: IncidentEvent
    status: InvestigationStatus
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    report: IncidentReport | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
