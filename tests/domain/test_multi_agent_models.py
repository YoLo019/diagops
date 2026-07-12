from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)


def test_multi_agent_contract_values_are_stable():
    assert [item.value for item in AgentExecutionLayer] == [
        "custom",
        "openai_agents_sdk",
    ]
    assert [item.value for item in CoordinationDecisionStatus] == [
        "agreement",
        "conflict",
        "agent_leads",
        "fallback",
    ]
    assert [item.value for item in MultiAgentRunStatus] == [
        "completed",
        "partial",
        "failed",
        "skipped",
    ]


def test_multi_agent_run_summary_defaults_failure_reason():
    summary = MultiAgentRunSummary(status="completed")

    assert summary.status == MultiAgentRunStatus.COMPLETED
    assert summary.failure_reason is None
