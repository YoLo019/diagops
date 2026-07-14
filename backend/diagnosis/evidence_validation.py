from __future__ import annotations

import math
from dataclasses import dataclass

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    RootCauseCandidate,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.results import ProviderResult, ProviderStatus


class EvidenceContractError(ValueError):
    """表示证据或引用在进入 RCA 边界前违反稳定契约。"""

    def __init__(self, code: str, reference: str | None = None) -> None:
        self.code = code
        self.reference = reference
        super().__init__(f"{code}: {reference}" if reference else code)


@dataclass(frozen=True)
class ValidatedEvidence:
    provider_results: list[ProviderResult]
    evidence: list[EvidenceItem]
    supporting_evidence: list[EvidenceItem]


_CAUSE_SUPPORT_PROVIDERS = {
    CauseType.DEPLOYMENT_REGRESSION: {
        EvidenceProvider.DEPLOY,
        EvidenceProvider.LOG,
        EvidenceProvider.METRIC,
    },
    CauseType.TRAFFIC_SPIKE: {EvidenceProvider.METRIC},
    CauseType.DOWNSTREAM_DEPENDENCY_FAILURE: {
        EvidenceProvider.DEPENDENCY,
        EvidenceProvider.LOG,
    },
    CauseType.DATABASE_SLOWDOWN: {EvidenceProvider.METRIC},
    CauseType.SINGLE_INSTANCE_ISSUE: {EvidenceProvider.METRIC},
}


def validate_investigation_evidence(
    provider_results: list[ProviderResult],
) -> ValidatedEvidence:
    """校验 Provider 结果并分离可用于结论的证据。"""
    evidence: list[EvidenceItem] = []
    supporting: list[EvidenceItem] = []
    seen_ids: set[str] = set()

    for result in provider_results:
        if (
            result.status in {ProviderStatus.FAILED, ProviderStatus.SKIPPED}
            and result.evidence_items
        ):
            raise EvidenceContractError("provider_status_evidence", result.provider)
        for item in result.evidence_items:
            if item.provider != result.provider:
                raise EvidenceContractError("provider_mismatch", item.id)
            if item.id in seen_ids:
                raise EvidenceContractError("duplicate_evidence_id", item.id)
            seen_ids.add(item.id)
            evidence.append(item)
            if (
                result.status in {ProviderStatus.SUCCESS, ProviderStatus.PARTIAL}
                and item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
                and item.kind != EvidenceKind.PROVIDER_ERROR
            ):
                supporting.append(item)

    return ValidatedEvidence(
        provider_results=list(provider_results),
        evidence=sorted(evidence, key=lambda item: item.timestamp),
        supporting_evidence=sorted(supporting, key=lambda item: item.timestamp),
    )


def validate_hypotheses(
    supporting_evidence: list[EvidenceItem], hypotheses: list[Hypothesis]
) -> None:
    """校验确定性结论的引用存在性、方向和因果语义。"""
    evidence_by_id = {item.id: item for item in supporting_evidence}
    if len(evidence_by_id) != len(supporting_evidence):
        raise EvidenceContractError("duplicate_evidence_id")

    for hypothesis in hypotheses:
        if not math.isfinite(hypothesis.confidence):
            raise EvidenceContractError("invalid_confidence", hypothesis.id)
        support = hypothesis.supporting_evidence_ids
        contradiction = hypothesis.contradicting_evidence_ids
        if len(support) != len(set(support)) or len(contradiction) != len(set(contradiction)):
            raise EvidenceContractError("duplicate_reference", hypothesis.id)
        if set(support) & set(contradiction):
            raise EvidenceContractError("inconsistent_reference_direction", hypothesis.id)
        _require_usable_references(evidence_by_id, [*support, *contradiction])

        if hypothesis.cause_type == CauseType.UNKNOWN:
            continue
        if not support:
            raise EvidenceContractError("missing_support", hypothesis.id)
        for evidence_id in support:
            _require_cause_support(
                hypothesis.cause_type, evidence_by_id[evidence_id], hypothesis.id
            )


def validate_agent_semantics(
    supporting_evidence: list[EvidenceItem],
    findings: list[AgentFinding],
    candidates: list[RootCauseCandidate],
) -> None:
    """校验 Agent finding/candidate 不会把 signal 或错向引用升级为根因。"""
    evidence_by_id = {item.id: item for item in supporting_evidence}
    finding_by_id = {finding.id: finding for finding in findings}
    if len(finding_by_id) != len(findings):
        raise EvidenceContractError("duplicate_finding_id")

    for finding in findings:
        if not math.isfinite(finding.confidence):
            raise EvidenceContractError("invalid_confidence", finding.id)
        _require_usable_references(evidence_by_id, finding.evidence_ids)
        if finding.finding_type == AgentFindingType.ROOT_CAUSE:
            if finding.related_cause_type in {None, CauseType.UNKNOWN}:
                raise EvidenceContractError("root_cause_without_cause", finding.id)
            for evidence_id in finding.evidence_ids:
                _require_cause_support(
                    finding.related_cause_type,
                    evidence_by_id[evidence_id],
                    finding.id,
                )

    ranks = [candidate.rank for candidate in candidates]
    if ranks != list(range(1, len(candidates) + 1)):
        raise EvidenceContractError("candidate_rank")

    for candidate in candidates:
        if not math.isfinite(candidate.confidence):
            raise EvidenceContractError("invalid_confidence", candidate.id)
        _require_usable_references(
            evidence_by_id,
            candidate.supporting_evidence_ids + candidate.contradicting_evidence_ids,
        )
        for evidence_id in candidate.supporting_evidence_ids:
            _require_cause_support(
                candidate.cause_type, evidence_by_id[evidence_id], candidate.id
            )
        for finding_id in candidate.supporting_finding_ids:
            finding = _finding(finding_by_id, finding_id)
            if finding.finding_type == AgentFindingType.SIGNAL:
                raise EvidenceContractError("signal_candidate_support", finding_id)
            if (
                finding.finding_type != AgentFindingType.ROOT_CAUSE
                or finding.related_cause_type != candidate.cause_type
            ):
                raise EvidenceContractError("candidate_support_mismatch", finding_id)
        for finding_id in candidate.contradicting_finding_ids:
            finding = _finding(finding_by_id, finding_id)
            if (
                finding.finding_type != AgentFindingType.CONTRADICTION
                or finding.related_cause_type != candidate.cause_type
            ):
                raise EvidenceContractError("candidate_contradiction_mismatch", finding_id)


def _require_usable_references(
    evidence_by_id: dict[str, EvidenceItem], references: list[str]
) -> None:
    missing = next((item for item in references if item not in evidence_by_id), None)
    if missing is not None:
        raise EvidenceContractError("unknown_or_unusable_evidence", missing)


def _require_cause_support(
    cause_type: CauseType, evidence: EvidenceItem, owner_id: str
) -> None:
    if evidence.provider not in _CAUSE_SUPPORT_PROVIDERS.get(cause_type, set()):
        raise EvidenceContractError("cause_support_mismatch", owner_id)


def _finding(
    finding_by_id: dict[str, AgentFinding], finding_id: str
) -> AgentFinding:
    try:
        return finding_by_id[finding_id]
    except KeyError as exc:
        raise EvidenceContractError("unknown_finding_id", finding_id) from exc
