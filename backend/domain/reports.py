from uuid import uuid4

from pydantic import BaseModel, Field, model_serializer, model_validator

from backend.domain.agent_findings import CriticAssessment, RootCauseCandidate
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import AuthorityMode, DiagnosticStatus


class IncidentReport(BaseModel):
    id: str = Field(default_factory=lambda: f"report-{uuid4().hex}")
    investigation_id: str
    summary: str
    timeline: list[dict[str, str]] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    markdown: str
    action_ids: list[str] = Field(default_factory=list)
    verification_suggestion_ids: list[str] = Field(default_factory=list)
    diagnoses: list[RootCauseCandidate] = Field(default_factory=list)
    alternatives: list[RootCauseCandidate] = Field(default_factory=list)
    diagnostic_status: DiagnosticStatus | None = None
    authority_mode: AuthorityMode | None = None
    critic_assessments: list[CriticAssessment] = Field(default_factory=list)
    critic_summary: str | None = Field(default=None, max_length=512)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=32)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    elapsed_time_ms: int = Field(default=0, ge=0)
    runtime_run_id: str | None = None

    @model_validator(mode="after")
    def validate_runtime_owner(self) -> "IncidentReport":
        if self.authority_mode == AuthorityMode.AGENT and self.runtime_run_id is None:
            raise ValueError("V11 report requires runtime_run_id")
        return self

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in (
                "diagnoses",
                "alternatives",
                "diagnostic_status",
                "authority_mode",
                "critic_assessments",
                "critic_summary",
                "evidence_gaps",
                "total_input_tokens",
                "total_output_tokens",
                "elapsed_time_ms",
                "runtime_run_id",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data
