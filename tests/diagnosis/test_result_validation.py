from datetime import UTC, datetime

import pytest

from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_agent_semantics,
)
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


def test_root_cause_attribution_rejects_unknown_evidence():
    evidence = EvidenceItem(
        id="ev-known",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 7, 15, tzinfo=UTC),
        summary="known evidence",
    )
    attribution = RootCauseAttribution(
        root_cause_occurred_at=evidence.timestamp,
        root_cause_component="checkout",
        root_cause_reason="process failure",
        supporting_evidence_ids=["ev-missing"],
    )

    with pytest.raises(EvidenceContractError, match="unknown_or_unusable_evidence"):
        validate_agent_semantics([evidence], [], [], [attribution])
