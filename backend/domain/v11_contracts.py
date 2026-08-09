"""Shared V11 publication contracts."""

from backend.domain.agent_findings import CoordinationReview
from backend.domain.multi_agent import (
    DiagnosticStatus,
    LeadAction,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)


def validate_v11_final_status(
    review: CoordinationReview,
    run: MultiAgentRunSummary,
) -> None:
    """Validate the final review/run/Lead combination before V11 publication."""
    if review.run_status != run.status:
        raise ValueError("V11 review and run status mismatch")
    if run.status not in {
        MultiAgentRunStatus.COMPLETED,
        MultiAgentRunStatus.PARTIAL,
    }:
        raise ValueError("V11 final projection requires a completed or partial run")
    if (
        review.diagnostic_status is None
        or run.diagnostic_status is None
        or review.diagnostic_status != run.diagnostic_status
    ):
        raise ValueError("V11 review and run diagnostic status mismatch")

    decision = review.lead_decision
    if decision is None:
        raise ValueError("V11 final projection requires a Lead decision")
    if review.diagnostic_status in {
        DiagnosticStatus.COMPLETE,
        DiagnosticStatus.PARTIAL,
    }:
        if decision.action != LeadAction.CONCLUDE or not decision.candidate_ids:
            raise ValueError("V11 final Lead decision is inconsistent with status")
        return
    if (
        decision.action != LeadAction.INCONCLUSIVE
        or decision.task_ids
        or decision.candidate_ids
    ):
        raise ValueError("V11 final Lead decision is inconsistent with status")
