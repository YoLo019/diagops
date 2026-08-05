from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class MemoryType(StrEnum):
    INVESTIGATION_SUMMARY = "investigation_summary"
    SERVICE_INCIDENT_SUMMARY = "service_incident_summary"
    HUMAN_FEEDBACK = "human_feedback"


class MemoryVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: f"mem-{uuid4().hex}")
    service: str
    environment: str
    memory_type: MemoryType
    summary: str
    source_investigation_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    verification_status: MemoryVerificationStatus = MemoryVerificationStatus.UNVERIFIED
    verified_at: datetime | None = None
    verified_by: str | None = Field(default=None, max_length=128)
    root_candidate_id: str | None = None
    verification_evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_verification(self) -> "MemoryItem":
        if self.verification_status == MemoryVerificationStatus.VERIFIED:
            if (
                self.verified_at is None
                or self.verified_by is None
                or self.root_candidate_id is None
            ):
                raise ValueError("verified memory requires verification provenance")
        return self
