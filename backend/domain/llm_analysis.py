from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class LLMAnalysis(BaseModel):
    id: str = Field(default_factory=lambda: f"llm-{uuid4().hex}")
    investigation_id: str
    summary: str
    missing_evidence: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    referenced_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        *,
        investigation_id: str,
        existing_evidence_ids: set[str],
        summary: str,
        missing_evidence: list[str] | None = None,
        risk_notes: list[str] | None = None,
        suggested_questions: list[str] | None = None,
        referenced_evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
    ) -> "LLMAnalysis":
        referenced_ids = referenced_evidence_ids or []
        unknown_ids = sorted(set(referenced_ids) - existing_evidence_ids)
        if unknown_ids:
            raise ValueError(
                "referenced_evidence_ids contain unknown evidence ids: "
                + ", ".join(unknown_ids)
            )

        return cls(
            investigation_id=investigation_id,
            summary=summary,
            missing_evidence=missing_evidence or [],
            risk_notes=risk_notes or [],
            suggested_questions=suggested_questions or [],
            referenced_evidence_ids=referenced_ids,
            created_at=created_at or datetime.now(UTC),
        )
