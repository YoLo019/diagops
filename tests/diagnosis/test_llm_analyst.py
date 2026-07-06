from datetime import UTC, datetime

import pytest

from backend.diagnosis.llm_analyst import ReadOnlyLlmAnalyst
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.llm_analysis import LLMAnalysis


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id="ev-log",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
            summary="5xx errors started after deploy.",
            payload={"error_rate": "12%"},
        ),
        EvidenceItem(
            id="ev-deploy",
            provider=EvidenceProvider.DEPLOY,
            kind=EvidenceKind.DEPLOYMENT,
            timestamp=datetime(2026, 7, 3, 13, 50, tzinfo=UTC),
            summary="checkout-service v2 deployed before the incident.",
            payload={"version": "v2"},
        ),
    ]


def _hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            cause_type=CauseType.DEPLOYMENT_REGRESSION,
            summary="Deployment regression is likely.",
            confidence=0.9,
            supporting_evidence_ids=["ev-log", "ev-deploy"],
        )
    ]


def test_disabled_analyst_returns_no_analysis():
    analysis = ReadOnlyLlmAnalyst(enabled=False).analyze(
        investigation_id="inv-1",
        evidence=_evidence(),
        hypotheses=_hypotheses(),
    )

    assert analysis is None


def test_stub_analyst_references_only_existing_evidence_ids():
    evidence = _evidence()
    analysis = ReadOnlyLlmAnalyst(enabled=True).analyze(
        investigation_id="inv-1",
        evidence=evidence,
        hypotheses=_hypotheses(),
    )

    assert analysis is not None
    assert set(analysis.referenced_evidence_ids) <= {item.id for item in evidence}
    assert analysis.missing_evidence
    assert analysis.suggested_questions


def test_invalid_referenced_evidence_id_raises_validation_error():
    with pytest.raises(ValueError, match="unknown evidence ids: ev-missing"):
        LLMAnalysis.create(
            investigation_id="inv-1",
            existing_evidence_ids={"ev-log"},
            summary="summary",
            referenced_evidence_ids=["ev-log", "ev-missing"],
        )
