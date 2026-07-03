from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class IncidentSource(StrEnum):
    SIMULATED = "simulated"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentEvent(BaseModel):
    source: IncidentSource
    service: str
    environment: str
    severity: Severity
    title: str
    description: str
    started_at: datetime
    time_window_minutes: int = 30
    signals: dict[str, str] = Field(default_factory=dict)
