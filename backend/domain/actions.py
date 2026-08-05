from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer, model_validator

from backend.domain.hypotheses import CauseType


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
    related_candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    runtime_run_id: str | None = None

    @model_validator(mode="after")
    def validate_action_contract(self) -> "RecommendedAction":
        if not self.supporting_evidence_ids:
            raise ValueError("supporting_evidence_ids must not be empty")
        needs_approval = self.risk_level in {
            ActionRiskLevel.MEDIUM,
            ActionRiskLevel.HIGH,
        }
        if needs_approval and not self.requires_approval:
            raise ValueError("medium and high risk actions require approval")
        if self.related_candidate_ids and self.runtime_run_id is None:
            raise ValueError("V11 action candidates require runtime_run_id")
        return self

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in ("related_candidate_ids", "runtime_run_id"):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data


class VerificationSuggestion(BaseModel):
    id: str = Field(default_factory=lambda: f"ver-{uuid4().hex}")
    title: str
    description: str
    expected_signal: str
    status: VerificationStatus = VerificationStatus.PENDING
    result_note: str | None = None
    result_evidence_ids: list[str] = Field(default_factory=list)
    related_action_ids: list[str] = Field(default_factory=list)
    related_cause_types: list[CauseType] = Field(default_factory=list)
    related_candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    runtime_run_id: str | None = None

    @model_validator(mode="after")
    def validate_runtime_owner(self) -> "VerificationSuggestion":
        if self.related_candidate_ids and self.runtime_run_id is None:
            raise ValueError("V11 verification candidates require runtime_run_id")
        return self

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in ("related_candidate_ids", "runtime_run_id"):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data
