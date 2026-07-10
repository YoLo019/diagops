from __future__ import annotations

from collections import Counter, defaultdict

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
)

LOW_CONFIDENCE = 0.5


def build_coordination_review(
    investigation_id: str,
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
) -> CoordinationReview:
    _validate_references(findings, evidence, hypotheses)
    return CoordinationReview(
        investigation_id=investigation_id,
        candidates=_build_candidates(findings, hypotheses),
    )


def conflicting_agent_names(
    baseline: Hypothesis,
    findings: list[AgentFinding],
) -> set[AgentName]:
    """Return only specialists whose active finding conflicts."""
    active = _active_sdk_findings(findings)
    conclusions = _active_conclusions(active)
    causes = {finding.related_cause_type for finding in conclusions.values()}
    conflicting = set()
    if _valid_baseline(baseline):
        conflicting.update(
            agent_name
            for agent_name, finding in conclusions.items()
            if finding.related_cause_type != baseline.cause_type
        )
    if len(causes) > 1:
        conflicting.update(conclusions)

    contradiction_targets = set(causes)
    if _valid_baseline(baseline):
        contradiction_targets.add(baseline.cause_type)
    for finding in active:
        if (
            finding.finding_type != AgentFindingType.CONTRADICTION
            or finding.related_cause_type not in contradiction_targets
        ):
            continue
        conflicting.add(finding.agent_name)
        conflicting.update(
            agent_name
            for agent_name, conclusion in conclusions.items()
            if conclusion.related_cause_type == finding.related_cause_type
        )
    return conflicting


def decide_hybrid_status(
    baseline: Hypothesis,
    findings: list[AgentFinding],
    run_status: MultiAgentRunStatus,
) -> CoordinationDecisionStatus:
    """Apply agreement, conflict, agent-leads, then fallback."""
    active = _active_sdk_findings(findings)
    conclusions = _active_conclusions(active)
    causes = {finding.related_cause_type for finding in conclusions.values()}
    support = Counter(finding.related_cause_type for finding in conclusions.values())
    selected = _otherwise_selected_cause(baseline, conclusions)
    contradicted = any(
        finding.finding_type == AgentFindingType.CONTRADICTION
        and finding.related_cause_type == selected
        for finding in active
    )

    if run_status in {MultiAgentRunStatus.FAILED, MultiAgentRunStatus.SKIPPED}:
        return CoordinationDecisionStatus.FALLBACK
    if (
        run_status == MultiAgentRunStatus.COMPLETED
        and _valid_baseline(baseline)
        and support[baseline.cause_type] >= 2
        and causes <= {baseline.cause_type}
        and not contradicted
    ):
        return CoordinationDecisionStatus.AGREEMENT
    if (
        (_valid_baseline(baseline) and any(cause != baseline.cause_type for cause in causes))
        or len(causes) > 1
        or contradicted
    ):
        return CoordinationDecisionStatus.CONFLICT
    if not _valid_baseline(baseline) and selected is not None:
        return CoordinationDecisionStatus.AGENT_LEADS
    return CoordinationDecisionStatus.FALLBACK


def build_hybrid_coordination_review(
    investigation_id: str,
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
    run_status: MultiAgentRunStatus,
    summary: str,
    uncertainty: str,
) -> CoordinationReview:
    """Validate references, build candidates, and assign status in code."""
    for finding in findings:
        if finding.investigation_id != investigation_id:
            raise ValueError("Finding investigation does not match review investigation")
    _validate_references(findings, evidence, hypotheses)
    active = _active_sdk_findings(findings)

    baseline = hypotheses[0] if hypotheses else None
    decision_status = (
        decide_hybrid_status(baseline, findings, run_status)
        if baseline is not None
        else CoordinationDecisionStatus.FALLBACK
    )
    if decision_status == CoordinationDecisionStatus.CONFLICT:
        selected_cause_type = None
    elif decision_status == CoordinationDecisionStatus.AGENT_LEADS:
        selected_cause_type = _otherwise_selected_cause(
            baseline, _active_conclusions(active)
        )
    else:
        selected_cause_type = baseline.cause_type if baseline is not None else None

    candidates = _build_candidates(active, hypotheses)
    by_cause = {candidate.cause_type: candidate for candidate in candidates}
    contradicted_causes = {
        finding.related_cause_type
        for finding in active
        if finding.finding_type == AgentFindingType.CONTRADICTION
    }
    for hypothesis in hypotheses:
        if (
            hypothesis.cause_type in contradicted_causes
            and hypothesis.cause_type not in by_cause
        ):
            candidate = _build_candidates([], [hypothesis])[0]
            candidates.append(candidate)
            by_cause[hypothesis.cause_type] = candidate
    for finding in active:
        candidate = by_cause.get(finding.related_cause_type)
        if finding.finding_type != AgentFindingType.CONTRADICTION or candidate is None:
            continue
        candidate.contradicting_finding_ids = _unique(
            candidate.contradicting_finding_ids + [finding.id]
        )
        candidate.contradicting_evidence_ids = _unique(
            candidate.contradicting_evidence_ids + finding.evidence_ids
        )
    if selected_cause_type is not None:
        selected_candidate = by_cause.get(selected_cause_type)
        if selected_candidate is None:
            selected_hypothesis = next(
                (
                    hypothesis
                    for hypothesis in hypotheses
                    if hypothesis.cause_type == selected_cause_type
                ),
                None,
            )
            if selected_hypothesis is not None:
                selected_candidate = _build_candidates([], [selected_hypothesis])[0]
                candidates.append(selected_candidate)
        if selected_candidate is not None:
            candidates.remove(selected_candidate)
            candidates.insert(0, selected_candidate)
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank
        candidate.summary = f"{candidate.cause_type} is candidate #{rank}."

    return CoordinationReview(
        investigation_id=investigation_id,
        candidates=candidates,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        run_status=run_status,
        decision_status=decision_status,
        baseline_cause_type=baseline.cause_type if baseline is not None else None,
        selected_cause_type=selected_cause_type,
        summary=summary,
        uncertainty=uncertainty,
    )


def _validate_references(
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
) -> None:
    evidence_ids = {item.id for item in evidence}
    references = [
        item_id for finding in findings for item_id in finding.evidence_ids
    ] + [
        item_id
        for hypothesis in hypotheses
        for item_id in (
            hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids
        )
    ]
    missing = next((item_id for item_id in references if item_id not in evidence_ids), None)
    if missing is not None:
        raise ValueError(f"Unknown evidence id: {missing}")


def _active_sdk_findings(findings: list[AgentFinding]) -> list[AgentFinding]:
    sdk_findings = [
        finding
        for finding in findings
        if finding.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    by_id: dict[str, AgentFinding] = {}
    for finding in sdk_findings:
        if finding.id in by_id:
            raise ValueError(f"Duplicate SDK finding id: {finding.id}")
        by_id[finding.id] = finding
    all_ids = {finding.id for finding in findings}
    for finding in sdk_findings:
        if finding.analysis_round != 2:
            continue
        revised = by_id.get(finding.revises_finding_id)
        if revised is None:
            if finding.revises_finding_id in all_ids:
                raise ValueError("A revision must target an SDK round 1 finding")
            raise ValueError(f"Unknown revision id: {finding.revises_finding_id}")
        if revised.analysis_round != 1:
            raise ValueError("A revision must target an SDK round 1 finding")
        if (
            revised.investigation_id != finding.investigation_id
            or revised.agent_name != finding.agent_name
        ):
            raise ValueError("A revision must target the same investigation and agent")

    revised_ids = {
        finding.revises_finding_id
        for finding in sdk_findings
        if finding.analysis_round == 2
    }
    return [finding for finding in sdk_findings if finding.id not in revised_ids]


def _active_conclusions(
    findings: list[AgentFinding],
) -> dict[AgentName, AgentFinding]:
    conclusions: dict[AgentName, AgentFinding] = {}
    for finding in findings:
        if (
            finding.finding_type != AgentFindingType.ROOT_CAUSE
            or finding.related_cause_type in {None, CauseType.UNKNOWN}
            or finding.confidence < LOW_CONFIDENCE
        ):
            continue
        previous = conclusions.get(finding.agent_name)
        if previous is None or finding.created_at >= previous.created_at:
            conclusions[finding.agent_name] = finding
    return conclusions


def _valid_baseline(baseline: Hypothesis) -> bool:
    return baseline.cause_type != CauseType.UNKNOWN and baseline.confidence >= LOW_CONFIDENCE


def _otherwise_selected_cause(
    baseline: Hypothesis,
    conclusions: dict[AgentName, AgentFinding],
) -> CauseType | None:
    if _valid_baseline(baseline):
        return baseline.cause_type
    support = Counter(finding.related_cause_type for finding in conclusions.values())
    if not support:
        return None
    [(cause_type, count), *rest] = support.most_common()
    if count < 2 or (rest and rest[0][1] == count):
        return None
    return cause_type


def _build_candidates(
    findings: list[AgentFinding], hypotheses: list[Hypothesis]
) -> list[RootCauseCandidate]:
    grouped: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    contradictions: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    for finding in findings:
        cause_type = finding.related_cause_type
        if cause_type is None or finding.finding_type == AgentFindingType.GAP:
            continue
        target = (
            contradictions
            if finding.finding_type == AgentFindingType.CONTRADICTION
            else grouped
        )
        target[cause_type].append(finding)

    candidates = [
        _candidate(cause_type, supporting, contradictions[cause_type])
        for cause_type, supporting in grouped.items()
    ]
    candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank
        candidate.summary = f"{candidate.cause_type} is candidate #{rank}."
    return candidates or [
        RootCauseCandidate(
            cause_type=hypothesis.cause_type,
            summary=f"{hypothesis.cause_type} is candidate #{rank}.",
            rank=rank,
            confidence=hypothesis.confidence,
            supporting_evidence_ids=hypothesis.supporting_evidence_ids,
            contradicting_evidence_ids=hypothesis.contradicting_evidence_ids,
            rationale="Seeded from existing RCA hypothesis.",
            uncertainty="limited specialist findings; use base RCA hypothesis.",
        )
        for rank, hypothesis in enumerate(hypotheses, start=1)
    ]


def _candidate(
    cause_type: CauseType,
    supporting: list[AgentFinding],
    contradicting: list[AgentFinding],
) -> RootCauseCandidate:
    confidence = sum(finding.confidence for finding in supporting) / len(supporting)
    confidence += 0.1 * (len(supporting) - 1)
    confidence -= 0.1 * len(contradicting)
    return RootCauseCandidate(
        cause_type=cause_type,
        summary=f"{cause_type} is candidate #1.",
        rank=1,
        confidence=max(0.0, min(1.0, confidence)),
        supporting_finding_ids=[finding.id for finding in supporting],
        contradicting_finding_ids=[finding.id for finding in contradicting],
        supporting_evidence_ids=_unique(
            item_id for finding in supporting for item_id in finding.evidence_ids
        ),
        contradicting_evidence_ids=_unique(
            item_id for finding in contradicting for item_id in finding.evidence_ids
        ),
        rationale=" ".join(finding.summary for finding in supporting),
        uncertainty=_uncertainty(supporting, contradicting),
    )


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _uncertainty(
    supporting: list[AgentFinding], contradicting: list[AgentFinding]
) -> str:
    if contradicting:
        return "Some specialist findings contradict this candidate."
    if len(supporting) == 1:
        return "Only one specialist currently supports this candidate."
    return "Supported by multiple specialist findings."
