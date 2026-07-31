from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ContextFactType(StrEnum):
    OBSERVATION = "observation"
    CORRELATION = "correlation"
    HYPOTHESIS = "hypothesis"
    CONCLUSION = "conclusion"
    MISSING_EVIDENCE = "missing_evidence"


class ContextFact(BaseModel):
    id: str = Field(default_factory=lambda: f"fact-{uuid4().hex}")
    source_agent: str
    fact_type: ContextFactType
    summary: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_evidence_for_supported_facts(self) -> "ContextFact":
        if self.fact_type != ContextFactType.MISSING_EVIDENCE and not self.evidence_ids:
            raise ValueError("evidence_ids required unless fact_type is missing_evidence")
        return self
