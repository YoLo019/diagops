from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus


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
    duration_ms: int = 0

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
