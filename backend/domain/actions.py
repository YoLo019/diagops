from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ActionType(StrEnum):
    CHECK = "check"
    NOTIFY_OWNER = "notify_owner"
    ROLLBACK_SUGGESTION = "rollback_suggestion"
    SCALE_SUGGESTION = "scale_suggestion"
    RESTART_SUGGESTION = "restart_suggestion"
    CONFIG_CHECK = "config_check"
    DEPENDENCY_CHECK = "dependency_check"
    MANUAL_FOLLOW_UP = "manual_follow_up"


class ActionRiskLevel(StrEnum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    DONE = "done"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RecommendedAction(BaseModel):
    id: str = Field(default_factory=lambda: f"act-{uuid4().hex}")
    action_type: ActionType
    title: str
    description: str
    risk_level: ActionRiskLevel
    requires_approval: bool
    supporting_evidence_ids: list[str]
    status: ActionStatus = ActionStatus.PROPOSED
    note: str | None = None

    @model_validator(mode="after")
    def validate_action_contract(self) -> "RecommendedAction":
        if not self.supporting_evidence_ids:
            raise ValueError("supporting_evidence_ids must not be empty")
        if self.risk_level in {ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH} and not self.requires_approval:
            raise ValueError("medium and high risk actions require approval")
        return self


class VerificationSuggestion(BaseModel):
    id: str = Field(default_factory=lambda: f"ver-{uuid4().hex}")
    title: str
    description: str
    expected_signal: str
    status: VerificationStatus = VerificationStatus.PENDING
    result_note: str | None = None
