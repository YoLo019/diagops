from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryType(StrEnum):
    INVESTIGATION_SUMMARY = "investigation_summary"
    SERVICE_INCIDENT_SUMMARY = "service_incident_summary"
    HUMAN_FEEDBACK = "human_feedback"


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: f"mem-{uuid4().hex}")
    service: str
    environment: str
    memory_type: MemoryType
    summary: str
    source_investigation_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
