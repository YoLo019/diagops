"""V11 public projection shared by reports and operator-facing APIs."""

import re

from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.agent_findings import (
    AgentFinding,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.agent_plan import AgentExecution, LeadDecision
from backend.domain.reports import IncidentReport
from backend.safety.redaction import redact_text

_V11_PRIVATE_TEXT = re.compile(
    r"(?is)(system\s+prompt|developer\s+message|private\s+reasoning|"
    r"chain\s+of\s+thought|thought\s+process|internal\s+prompt|"
    r"原始提示词|私有推理|思维链|系统提示)"
)


def scrub_v11_text(text: str | None) -> str:
    """移除 V11 私有推理标记，并继续使用通用凭据清洗。"""
    if not text:
        return ""
    if _V11_PRIVATE_TEXT.search(text):
        return "[内部推理内容已省略]"
    return redact_text(text)


def public_v11_candidate(candidate: RootCauseCandidate) -> RootCauseCandidate:
    return candidate.model_copy(
        update={
            "affected_entity": scrub_v11_text(candidate.affected_entity)
            if candidate.affected_entity is not None
            else None,
            "failure_class": scrub_v11_text(candidate.failure_class)
            if candidate.failure_class is not None
            else None,
            "failure_mechanism": scrub_v11_text(candidate.failure_mechanism)
            if candidate.failure_mechanism is not None
            else None,
            "summary": scrub_v11_text(candidate.summary),
            "rationale": scrub_v11_text(candidate.rationale),
            "uncertainty": scrub_v11_text(candidate.uncertainty),
        }
    )


def public_v11_finding(finding: AgentFinding) -> AgentFinding:
    return finding.model_copy(
        update={
            "summary": scrub_v11_text(finding.summary),
            "rationale": scrub_v11_text(finding.rationale),
            "gaps": [scrub_v11_text(item) for item in finding.gaps],
            "affected_entity": scrub_v11_text(finding.affected_entity)
            if finding.affected_entity is not None
            else None,
            "failure_mechanism": scrub_v11_text(finding.failure_mechanism)
            if finding.failure_mechanism is not None
            else None,
        }
    )


def public_v11_assessment(assessment):
    return assessment.model_copy(
        update={
            "checks": [
                check.model_copy(
                    update={
                        "summary": scrub_v11_text(check.summary),
                        "gap": scrub_v11_text(check.gap)
                        if check.gap is not None
                        else None,
                    }
                )
                for check in assessment.checks
            ],
            "gap": scrub_v11_text(assessment.gap)
            if assessment.gap is not None
            else None,
            "summary": scrub_v11_text(assessment.summary),
        }
    )


def public_v11_lead_decision(decision: LeadDecision) -> LeadDecision:
    return decision.model_copy(
        update={
            "summary": scrub_v11_text(decision.summary),
            "stop_reason": scrub_v11_text(decision.stop_reason)
            if decision.stop_reason is not None
            else None,
            **({"uncertainty": scrub_v11_text(decision.uncertainty)
                if decision.uncertainty is not None else None}
               if hasattr(decision, "uncertainty") else {}),
        }
    )


def public_v11_review(review: CoordinationReview) -> CoordinationReview:
    return review.model_copy(
        update={
            "candidates": [public_v11_candidate(item) for item in review.candidates],
            "critic_assessments": [
                public_v11_assessment(item) for item in review.critic_assessments
            ],
            "final_decision": (
                public_v11_lead_decision(review.final_decision)
                if review.final_decision is not None else None
            ),
            "lead_decision": (
                public_v11_lead_decision(review.lead_decision)
                if review.lead_decision is not None
                else None
            ),
            "summary": scrub_v11_text(review.summary),
            "uncertainty": scrub_v11_text(review.uncertainty),
            "stop_reason": scrub_v11_text(review.stop_reason)
            if review.stop_reason is not None
            else None,
        }
    )


def public_v11_action(action: RecommendedAction) -> RecommendedAction:
    return action.model_copy(
        update={
            "title": scrub_v11_text(action.title),
            "description": scrub_v11_text(action.description),
            "note": scrub_v11_text(action.note) if action.note is not None else None,
        }
    )


def public_v11_verification(
    suggestion: VerificationSuggestion,
) -> VerificationSuggestion:
    return suggestion.model_copy(
        update={
            "title": scrub_v11_text(suggestion.title),
            "description": scrub_v11_text(suggestion.description),
            "expected_signal": scrub_v11_text(suggestion.expected_signal),
            "result_note": (
                scrub_v11_text(suggestion.result_note)
                if suggestion.result_note is not None
                else None
            ),
        }
    )


def public_v11_execution(execution: AgentExecution) -> AgentExecution:
    return execution.model_copy(
        update={
            "summary": scrub_v11_text(execution.summary)
            if execution.summary is not None
            else None,
            "error_message": scrub_v11_text(execution.error_message)
            if execution.error_message is not None
            else None,
        }
    )


def public_v11_report(report: IncidentReport) -> IncidentReport:
    return report.model_copy(
        update={
            "summary": scrub_v11_text(report.summary),
            "markdown": scrub_v11_text(report.markdown),
            "timeline": [
                {
                    **item,
                    "event": scrub_v11_text(item.get("event")),
                }
                for item in report.timeline
            ],
            "diagnoses": [public_v11_candidate(item) for item in report.diagnoses],
            "alternatives": [
                public_v11_candidate(item) for item in report.alternatives
            ],
            "critic_assessments": [
                public_v11_assessment(item) for item in report.critic_assessments
            ],
            "critic_summary": (
                scrub_v11_text(report.critic_summary)
                if report.critic_summary is not None
                else None
            ),
            "evidence_gaps": [scrub_v11_text(item) for item in report.evidence_gaps],
        }
    )


def public_v11_graph_seed(
    evidence,
    findings: list[AgentFinding],
    candidates: list[RootCauseCandidate],
    *,
    review: CoordinationReview,
) -> dict[str, list[dict[str, str]]]:
    """构造只包含公共标签的 V11 evidence→finding→candidate→review graph。"""
    nodes: dict[str, dict[str, str]] = {}
    edges: list[dict[str, str]] = []

    for item in evidence:
        nodes[item.id] = {
            "id": item.id,
            "label": scrub_v11_text(item.summary),
            "type": "evidence",
        }
    for finding in findings:
        agent_id = str(finding.agent_name)
        nodes[agent_id] = {"id": agent_id, "label": agent_id, "type": "agent"}
        nodes[finding.id] = {
            "id": finding.id,
            "label": scrub_v11_text(finding.summary),
            "type": "finding",
        }
        edges.append({"source": agent_id, "target": finding.id, "relation": "produced"})
        edges.extend(
            {"source": finding.id, "target": evidence_id, "relation": "cites"}
            for evidence_id in finding.evidence_ids
        )
    for candidate in candidates:
        nodes[candidate.id] = {
            "id": candidate.id,
            "label": scrub_v11_text(candidate.summary),
            "type": "candidate",
        }
        edges.extend(
            {"source": finding_id, "target": candidate.id, "relation": "supports"}
            for finding_id in candidate.supporting_finding_ids
        )
        edges.extend(
            {"source": finding_id, "target": candidate.id, "relation": "contradicts"}
            for finding_id in candidate.contradicting_finding_ids
        )

    for assessment in review.critic_assessments:
        nodes[assessment.id] = {
            "id": assessment.id,
            "label": scrub_v11_text(assessment.summary),
            "type": "critic_assessment",
        }
        if assessment.candidate_id in nodes:
            edges.append(
                {
                    "source": assessment.id,
                    "target": assessment.candidate_id,
                    "relation": f"critic_{assessment.verdict}",
                }
            )
    decision = review.final_decision or review.lead_decision
    if decision is not None:
        lead_id = f"final-{review.id}" if review.final_decision else f"lead-{review.id}"
        nodes[lead_id] = {
            "id": lead_id,
            "label": scrub_v11_text(decision.summary),
            "type": "final_decision" if review.final_decision else "lead_decision",
        }
        edges.extend(
            {"source": lead_id, "target": candidate_id, "relation": "accepted"}
            for candidate_id in decision.candidate_ids
            if candidate_id in nodes
        )

    node_ids = set(nodes)
    return {
        "nodes": list(nodes.values()),
        "edges": [
            edge
            for edge in edges
            if edge["source"] in node_ids and edge["target"] in node_ids
        ],
    }
