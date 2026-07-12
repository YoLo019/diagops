from enum import StrEnum

from pydantic import BaseModel


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


class MultiAgentRunSummary(BaseModel):
    status: MultiAgentRunStatus
    failure_reason: str | None = None
