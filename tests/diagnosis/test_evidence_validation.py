from datetime import UTC, datetime

import pytest

from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_agent_semantics,
    validate_hypotheses,
    validate_investigation_evidence,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    RootCauseCandidate,
)
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.results import ProviderResult, ProviderStatus


def _evidence(
    evidence_id: str,
    provider: EvidenceProvider = EvidenceProvider.METRIC,
    *,
    status: EvidenceStatus = EvidenceStatus.SUCCESS,
) -> EvidenceItem:
    kinds = {
        EvidenceProvider.LOG: EvidenceKind.LOG_PATTERN,
        EvidenceProvider.METRIC: EvidenceKind.METRIC_TREND,
        EvidenceProvider.DEPLOY: EvidenceKind.DEPLOYMENT,
        EvidenceProvider.DEPENDENCY: EvidenceKind.DEPENDENCY_HEALTH,
        EvidenceProvider.SERVICE_CATALOG: EvidenceKind.SERVICE_METADATA,
        EvidenceProvider.RELATED_ALERT: EvidenceKind.RELATED_ALERT,
    }
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kinds[provider],
        status=status,
        timestamp=datetime(2026, 7, 13, tzinfo=UTC),
        summary="bounded evidence",
    )


def test_duplicate_evidence_ids_fail_closed() -> None:
    evidence = _evidence("ev-duplicate")

    with pytest.raises(EvidenceContractError) as exc_info:
        validate_investigation_evidence(
            [
                ProviderResult(provider=evidence.provider, evidence_items=[evidence]),
                ProviderResult(provider=evidence.provider, evidence_items=[evidence]),
            ]
        )

    assert exc_info.value.code == "duplicate_evidence_id"
    assert exc_info.value.reference == "ev-duplicate"


@pytest.mark.parametrize("status", [ProviderStatus.FAILED, ProviderStatus.SKIPPED])
def test_failed_or_skipped_provider_cannot_contain_evidence(status: ProviderStatus) -> None:
    with pytest.raises(EvidenceContractError, match="provider_status_evidence"):
        validate_investigation_evidence(
            [
                ProviderResult(
                    provider=EvidenceProvider.LOG,
                    status=status,
                    evidence_items=[_evidence("ev-log", EvidenceProvider.LOG)],
                )
            ]
        )


def test_evidence_provider_must_match_parent_result() -> None:
    with pytest.raises(EvidenceContractError, match="provider_mismatch"):
        validate_investigation_evidence(
            [
                ProviderResult(
                    provider=EvidenceProvider.LOG,
                    evidence_items=[_evidence("ev-metric")],
                )
            ]
        )


def test_concrete_hypothesis_requires_compatible_support() -> None:
    validated = validate_investigation_evidence(
        [
            ProviderResult(
                provider=EvidenceProvider.DEPENDENCY,
                evidence_items=[
                    _evidence("ev-dependency", EvidenceProvider.DEPENDENCY)
                ],
            )
        ]
    )
    hypothesis = Hypothesis(
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="unsupported deployment",
        confidence=0.9,
        supporting_evidence_ids=["ev-dependency"],
    )

    with pytest.raises(EvidenceContractError, match="cause_support_mismatch"):
        validate_hypotheses(validated.supporting_evidence, [hypothesis])


def test_failed_evidence_cannot_support_hypothesis() -> None:
    failed = _evidence(
        "ev-failed", EvidenceProvider.METRIC, status=EvidenceStatus.FAILED
    )
    validated = validate_investigation_evidence(
        [
            ProviderResult(
                provider=EvidenceProvider.METRIC,
                status=ProviderStatus.PARTIAL,
                evidence_items=[failed],
            )
        ]
    )
    hypothesis = Hypothesis(
        cause_type=CauseType.TRAFFIC_SPIKE,
        summary="failed evidence is not support",
        confidence=0.8,
        supporting_evidence_ids=[failed.id],
    )

    with pytest.raises(EvidenceContractError, match="unknown_or_unusable_evidence"):
        validate_hypotheses(validated.supporting_evidence, [hypothesis])


def test_unknown_hypothesis_without_support_is_valid() -> None:
    validate_hypotheses(
        [],
        [
            Hypothesis(
                cause_type=CauseType.UNKNOWN,
                summary="insufficient evidence",
                confidence=0.2,
            )
        ],
    )


def test_signal_finding_cannot_support_root_cause_candidate() -> None:
    evidence = _evidence("ev-metric")
    finding = AgentFinding(
        id="finding-signal",
        investigation_id="inv-1",
        agent_name=AgentName.METRIC,
        finding_type=AgentFindingType.SIGNAL,
        summary="metric signal",
        confidence=0.8,
        evidence_ids=[evidence.id],
        related_cause_type=CauseType.TRAFFIC_SPIKE,
    )
    candidate = RootCauseCandidate(
        cause_type=CauseType.TRAFFIC_SPIKE,
        summary="traffic candidate",
        rank=1,
        confidence=0.8,
        supporting_finding_ids=[finding.id],
        supporting_evidence_ids=[evidence.id],
    )

    with pytest.raises(EvidenceContractError, match="signal_candidate_support"):
        validate_agent_semantics([evidence], [finding], [candidate])


def test_candidate_ranks_are_unique_and_contiguous() -> None:
    evidence = _evidence("ev-metric")
    finding = AgentFinding(
        id="finding-root",
        investigation_id="inv-1",
        agent_name=AgentName.METRIC,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="traffic root cause",
        confidence=0.8,
        evidence_ids=[evidence.id],
        related_cause_type=CauseType.TRAFFIC_SPIKE,
    )
    candidates = [
        RootCauseCandidate(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="first",
            rank=1,
            confidence=0.8,
            supporting_finding_ids=[finding.id],
            supporting_evidence_ids=[evidence.id],
        ),
        RootCauseCandidate(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="duplicate",
            rank=1,
            confidence=0.7,
            supporting_finding_ids=[finding.id],
            supporting_evidence_ids=[evidence.id],
        ),
    ]

    with pytest.raises(EvidenceContractError, match="candidate_rank"):
        validate_agent_semantics([evidence], [finding], candidates)
