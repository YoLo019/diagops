from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from backend.safety.redaction import assert_safe_label


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
    time_window_minutes: int = Field(default=30, gt=0)
    signals: dict[str, str] = Field(default_factory=dict)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        """在共享事件边界拒绝凭据形态的 environment 标签。"""
        assert_safe_label(value)
        return value
