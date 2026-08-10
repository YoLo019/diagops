from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_serializer

from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.domain.multi_agent import FailureCategory


class ProviderStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderResult(BaseModel):
    provider: EvidenceProvider
    status: ProviderStatus = ProviderStatus.SUCCESS
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    error_message: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    failure_category: FailureCategory | None = None

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.failure_category is None and "failure_category" not in self.model_fields_set:
            data.pop("failure_category", None)
        return data

    def to_error_evidence(self) -> EvidenceItem:
        status = EvidenceStatus(self.status)
        return EvidenceItem(
            provider=self.provider,
            kind=EvidenceKind.PROVIDER_ERROR,
            status=status,
            timestamp=datetime.now(UTC),
            summary=f"{self.provider} provider {self.status}",
            payload={
                "provider": self.provider,
                "provider_status": self.status,
                "duration_ms": self.duration_ms,
            },
            confidence=1.0,
            error_message=self.error_message,
        )
