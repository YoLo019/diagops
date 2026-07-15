from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class AgentExecutionLayer(StrEnum):
    CUSTOM = "custom"
    OPENAI_AGENTS_SDK = "openai_agents_sdk"


class CoordinationDecisionStatus(StrEnum):
    AGREEMENT = "agreement"
    CONFLICT = "conflict"
    AGENT_LEADS = "agent_leads"
    FALLBACK = "fallback"


class MultiAgentRunStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class InvestigationStrategy(StrEnum):
    FIXED = "fixed"
    ADAPTIVE = "adaptive"


class AdaptiveRunStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


class AdaptiveStopReason(StrEnum):
    SUFFICIENT_EVIDENCE = "sufficient_evidence"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_NEW_EVIDENCE = "no_new_evidence"
    DUPLICATE_QUERY = "duplicate_query"
    ROUND_LIMIT = "round_limit"
    TIMEOUT = "timeout"
    FAILED = "failed"


class ModelProvider(StrEnum):
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class ResultValidationCategory(StrEnum):
    TASK_CONTRACT = "task_contract"
    EXECUTION_CONTRACT = "execution_contract"
    FINDING_CONTRACT = "finding_contract"
    FINDING_REVISION_CONTRACT = "finding_revision_contract"
    REVIEW_ATTRIBUTION = "review_attribution"
    REVIEW_CONTRACT = "review_contract"
    SEMANTIC_REFERENCE = "semantic_reference"
    RUN_STATUS_CONTRACT = "run_status_contract"


class ExecutionStepKind(StrEnum):
    INITIAL_COORDINATION = "initial_coordination"
    SPECIALIST_COLLECTION = "specialist_collection"
    SPECIALIST_RECOLLECTION = "specialist_recollection"
    FINAL_SYNTHESIS = "final_synthesis"
    REFERENCE_VALIDATION = "reference_validation"
    HYBRID_ARBITRATION = "hybrid_arbitration"
    REVIEW_PERSISTENCE = "review_persistence"
    RESULT_VALIDATION = "result_validation"


class FailureCategory(StrEnum):
    NONE = "none"
    NOT_CONFIGURED = "not_configured"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    TRANSPORT = "transport"
    INVALID_OUTPUT = "invalid_output"
    INVALID_REFERENCE = "invalid_reference"
    MISSING_SPECIALIST = "missing_specialist"
    UNSAFE_OUTPUT = "unsafe_output"
    PERSISTENCE = "persistence"
    UNKNOWN = "unknown"


class StabilizationCategory(StrEnum):
    REFERENCE_VALIDATION = "reference_validation"
    UNSAFE_OUTPUT = "unsafe_output"
    CANCELLED_OR_TIMEOUT = "cancelled_or_timeout"
    PROVIDER_OR_SDK_TRANSPORT = "provider_or_sdk_transport"
    COORDINATOR_OUTPUT_CONTRACT = "coordinator_output_contract"
    SPECIALIST_OUTPUT_CONTRACT = "specialist_output_contract"
    EVIDENCE_CAUSE_MAPPING = "evidence_cause_mapping"
    GENUINE_CONFLICT = "genuine_conflict"
    MISSING_SPECIALIST = "missing_specialist"
    FINAL_SYNTHESIS = "final_synthesis"
    REVIEW_PERSISTENCE = "review_persistence"
    HYBRID_CONTRACT = "hybrid_contract"
    RESULT_VALIDATION = "result_validation"
    UNKNOWN = "unknown"


class MultiAgentRunSummary(BaseModel):
    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    status: MultiAgentRunStatus
    failure_reason: str | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    primary_stabilization_category: StabilizationCategory | None = None
    secondary_stabilization_categories: list[StabilizationCategory] = Field(
        default_factory=list
    )
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    adaptive_status: AdaptiveRunStatus = AdaptiveRunStatus.NOT_APPLICABLE
    adaptive_stop_reason: AdaptiveStopReason | None = None
    tool_call_count: int = Field(default=0, ge=0)
    max_tool_calls_per_specialist: int = Field(default=3, ge=1)
    max_total_tool_calls: int = Field(default=8, ge=1)

    @model_serializer(mode="wrap", when_used="json")
    def serialize_without_unset_reliability_fields(self, handler):
        data = handler(self)
        for field_name in (
            "model_provider",
            "model_name",
            "primary_stabilization_category",
            "secondary_stabilization_categories",
            "strategy",
            "adaptive_status",
            "adaptive_stop_reason",
            "tool_call_count",
            "max_tool_calls_per_specialist",
            "max_total_tool_calls",
        ):
            if field_name not in self.model_fields_set:
                data.pop(field_name, None)
        return data
