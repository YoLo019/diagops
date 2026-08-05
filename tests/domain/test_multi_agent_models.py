from backend.domain.multi_agent import (
    AdaptiveRunStatus,
    AdaptiveStopReason,
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
    ResultValidationCategory,
    StabilizationCategory,
)


def test_multi_agent_contract_values_are_stable():
    assert [item.value for item in InvestigationStrategy] == ["fixed", "adaptive"]
    assert [item.value for item in AdaptiveRunStatus] == [
        "not_applicable", "completed", "degraded", "skipped"
    ]
    assert [item.value for item in AdaptiveStopReason] == [
        "sufficient_evidence", "budget_exhausted", "no_new_evidence",
        "duplicate_query", "round_limit", "timeout", "failed",
    ]
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
    assert [item.value for item in ModelProvider] == [
        "openai",
        "deepseek",
        "openai_compatible",
    ]
    assert [item.value for item in ExecutionStepKind] == [
        "initial_coordination",
        "specialist_collection",
        "specialist_recollection",
        "final_synthesis",
        "reference_validation",
        "hybrid_arbitration",
        "review_persistence",
        "result_validation",
        "lead_planning",
        "investigator_analysis",
        "critic_review",
        "lead_adjudication",
    ]
    assert [item.value for item in FailureCategory] == [
        "none",
        "not_configured",
        "authentication",
        "rate_limit",
        "quota",
        "timeout",
        "cancelled",
        "transport",
        "invalid_output",
        "invalid_reference",
        "missing_specialist",
        "unsafe_output",
        "persistence",
        "unknown",
    ]
    assert [item.value for item in StabilizationCategory] == [
        "reference_validation",
        "unsafe_output",
        "cancelled_or_timeout",
        "provider_or_sdk_transport",
        "coordinator_output_contract",
        "specialist_output_contract",
        "evidence_cause_mapping",
        "genuine_conflict",
        "missing_specialist",
        "final_synthesis",
        "review_persistence",
        "hybrid_contract",
        "result_validation",
        "unknown",
    ]


def test_result_validation_contract_values_are_frozen():
    assert {item.value for item in ResultValidationCategory} == {
        "task_contract",
        "execution_contract",
        "finding_contract",
        "finding_revision_contract",
        "review_attribution",
        "review_contract",
        "semantic_reference",
        "run_status_contract",
    }
    assert ExecutionStepKind.RESULT_VALIDATION == "result_validation"
    assert StabilizationCategory.RESULT_VALIDATION == "result_validation"


def test_multi_agent_run_summary_defaults_failure_reason():
    summary = MultiAgentRunSummary(status="completed")
    another = MultiAgentRunSummary(status="completed")

    assert summary.status == MultiAgentRunStatus.COMPLETED
    assert summary.failure_reason is None
    assert summary.model_provider is None
    assert summary.model_name is None
    assert summary.primary_stabilization_category is None
    assert summary.secondary_stabilization_categories == []
    assert summary.secondary_stabilization_categories is not (
        another.secondary_stabilization_categories
    )
    assert summary.strategy == InvestigationStrategy.FIXED
    assert summary.adaptive_status == AdaptiveRunStatus.NOT_APPLICABLE
    assert summary.adaptive_stop_reason is None
    assert summary.tool_call_count == 0
    assert summary.max_tool_calls_per_specialist == 3
    assert summary.max_total_tool_calls == 8


def test_multi_agent_run_summary_uses_pydantic_28_protected_namespaces():
    assert MultiAgentRunSummary.model_config["protected_namespaces"] == (
        "model_validate",
        "model_dump",
    )


def test_multi_agent_run_summary_old_payload_keeps_json_shape():
    summary = MultiAgentRunSummary(status="completed")

    assert summary.model_dump(mode="json") == {
        "status": "completed",
        "failure_reason": None,
    }


def test_multi_agent_run_summary_python_dump_keeps_new_defaults():
    summary = MultiAgentRunSummary(status="completed")

    dumped = summary.model_dump(mode="python")

    assert dumped["model_provider"] is None
    assert dumped["model_name"] is None
    assert dumped["primary_stabilization_category"] is None
    assert dumped["secondary_stabilization_categories"] == []


def test_multi_agent_run_summary_json_dump_keeps_explicit_defaults():
    summary = MultiAgentRunSummary(
        status="completed",
        model_provider=None,
        model_name=None,
        primary_stabilization_category=None,
        secondary_stabilization_categories=[],
    )

    dumped = summary.model_dump(mode="json")

    assert dumped["model_provider"] is None
    assert dumped["model_name"] is None
    assert dumped["primary_stabilization_category"] is None
    assert dumped["secondary_stabilization_categories"] == []


def test_multi_agent_run_summary_dumps_enum_values_as_json_strings():
    summary = MultiAgentRunSummary(
        status=MultiAgentRunStatus.PARTIAL,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        primary_stabilization_category=StabilizationCategory.FINAL_SYNTHESIS,
        secondary_stabilization_categories=[
            StabilizationCategory.REFERENCE_VALIDATION
        ],
    )

    dumped = summary.model_dump(mode="json")

    assert dumped["status"] == "partial"
    assert dumped["model_provider"] == "openai"
    assert dumped["primary_stabilization_category"] == "final_synthesis"
    assert dumped["secondary_stabilization_categories"] == ["reference_validation"]
