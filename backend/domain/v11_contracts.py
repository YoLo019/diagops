"""Shared V11 publication contracts."""

from backend.domain.agent_findings import CoordinationReview
from backend.domain.multi_agent import (
    DiagnosticStatus,
    LeadAction,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.domain.runtime import RuntimeRunStatus


def validate_v11_final_status(
    review: CoordinationReview,
    run: MultiAgentRunSummary,
    *,
    durable_status: RuntimeRunStatus | None = None,
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

    expected_run_status = {
        DiagnosticStatus.COMPLETE: MultiAgentRunStatus.COMPLETED,
        DiagnosticStatus.PARTIAL: MultiAgentRunStatus.PARTIAL,
        DiagnosticStatus.INCONCLUSIVE: MultiAgentRunStatus.COMPLETED,
    }.get(review.diagnostic_status)
    if expected_run_status is None or run.status != expected_run_status:
        raise ValueError("V11 diagnostic status and run status combination is invalid")
    if durable_status is not None and durable_status != RuntimeRunStatus.COMPLETED:
        raise ValueError("V11 projection requires a completed durable RuntimeRun")

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


def validate_v11_report_projection(
    review: CoordinationReview,
    report: IncidentReport,
) -> None:
    """确保报告候选引用只来自同一份 Lead 权威裁决。"""
    if report.investigation_id != review.investigation_id:
        raise ValueError("V11 report investigation does not match coordination review")
    if report.runtime_run_id != review.runtime_run_id:
        raise ValueError("V11 report runtime owner does not match coordination review")
    if report.diagnostic_status != review.diagnostic_status:
        raise ValueError("V11 report diagnostic status does not match coordination review")

    if review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE:
        if report.diagnoses or report.alternatives:
            raise ValueError("V11 inconclusive report cannot publish candidate outputs")
        return

    lead_ids = review.authoritative_candidate_ids
    diagnosis_ids = [candidate.id for candidate in report.diagnoses]
    if diagnosis_ids != lead_ids:
        raise ValueError("V11 report diagnoses do not match Lead candidate references")

    accepted_ids = set(lead_ids)
    expected_alternative_ids = [
        candidate.id
        for candidate in review.candidates
        if candidate.id not in accepted_ids
    ]
    alternative_ids = [candidate.id for candidate in report.alternatives]
    if alternative_ids != expected_alternative_ids:
        raise ValueError(
            "V11 report alternatives do not match review candidate references"
        )
