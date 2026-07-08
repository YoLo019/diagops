from __future__ import annotations

from collections import defaultdict

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis


def build_coordination_review(
    investigation_id: str,
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
) -> CoordinationReview:
    evidence_ids = {item.id for item in evidence}
    for finding in findings:
        missing = [item_id for item_id in finding.evidence_ids if item_id not in evidence_ids]
        if missing:
            raise ValueError(f"Unknown evidence id: {missing[0]}")
    for hypothesis in hypotheses:
        missing = [
            item_id
            for item_id in (
                hypothesis.supporting_evidence_ids
                + hypothesis.contradicting_evidence_ids
            )
            if item_id not in evidence_ids
        ]
        if missing:
            raise ValueError(f"Unknown evidence id: {missing[0]}")

    grouped: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    contradictions: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    for finding in findings:
        cause_type = finding.related_cause_type
        if cause_type is None or finding.finding_type == AgentFindingType.GAP:
            continue
        if finding.finding_type == AgentFindingType.CONTRADICTION:
            contradictions[cause_type].append(finding)
        else:
            grouped[cause_type].append(finding)

    candidates = [
        _candidate(cause_type, supporting, contradictions[cause_type])
        for cause_type, supporting in grouped.items()
    ]
    candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank
        candidate.summary = f"{candidate.cause_type} is candidate #{rank}."

    if not candidates:
        candidates = [
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

    return CoordinationReview(investigation_id=investigation_id, candidates=candidates)


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
