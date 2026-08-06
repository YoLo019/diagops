from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvenance,
    EvidenceProvider,
    EvidenceScope,
    EvidenceSourceClass,
)
from backend.domain.memory import MemoryItem, MemoryType, MemoryVerificationStatus
from backend.domain.multi_agent import (
    AuthorityMode,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)


def _provenance(**updates) -> EvidenceProvenance:
    values = {
        "source_class": EvidenceSourceClass.PUBLIC_DATASET,
        "provider_profile": "rcaeval-local",
        "source_artifact_id": "case-0001",
        "source_artifact_hash": "a" * 64,
        "adapter_version": "v1",
    }
    values.update(updates)
    return EvidenceProvenance(**values)


def test_evidence_provenance_round_trip_and_bounded_fields() -> None:
    provenance = _provenance()

    restored = EvidenceProvenance.model_validate(provenance.model_dump(mode="json"))

    assert restored == provenance
    with pytest.raises(ValidationError):
        _provenance(source_artifact_hash="not-a-sha256")
    with pytest.raises(ValidationError):
        _provenance(source_artifact_hash="A" * 64)
    with pytest.raises(ValidationError):
        _provenance(provider_profile="")
    with pytest.raises(ValidationError):
        _provenance(adapter_version="")


def test_evidence_scope_window_must_appear_together_and_be_ordered() -> None:
    start = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 8, 1, 11, 0, tzinfo=UTC)

    scope = EvidenceScope(
        entity_ids=["checkout-service"],
        observed_at=start,
        window_start=start,
        window_end=end,
        signal_type="latency",
    )
    assert EvidenceScope.model_validate(scope.model_dump(mode="json")) == scope
    assert EvidenceScope().entity_ids == []

    with pytest.raises(ValidationError, match="together"):
        EvidenceScope(window_start=start)
    with pytest.raises(ValidationError, match="together"):
        EvidenceScope(window_end=end)
    with pytest.raises(ValidationError, match="later"):
        EvidenceScope(window_start=end, window_end=start)
    with pytest.raises(ValidationError):
        EvidenceScope(entity_ids=[f"entity-{index}" for index in range(21)])


@pytest.mark.parametrize(
    ("provider", "kind"),
    [
        (EvidenceProvider.TRACE, EvidenceKind.TRACE_PATH),
        (EvidenceProvider.TRACE, EvidenceKind.TRACE_ERROR),
        (EvidenceProvider.RUNTIME_STATE, EvidenceKind.RUNTIME_STATE),
        (EvidenceProvider.VERIFIED_INCIDENT, EvidenceKind.VERIFIED_INCIDENT),
    ],
)
def test_v11_evidence_provider_kind_round_trip_with_scope_and_provenance(
    provider: EvidenceProvider, kind: EvidenceKind
) -> None:
    item = EvidenceItem(
        provider=provider,
        kind=kind,
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        summary="bounded v11 evidence",
        scope=EvidenceScope(entity_ids=["checkout-service"]),
        provenance=_provenance(),
        runtime_run_id="run-evidence-owner",
    )

    restored = EvidenceItem.model_validate(item.model_dump(mode="json"))

    assert restored == item
    assert restored.provider is provider
    assert restored.kind is kind


def test_legacy_evidence_without_owner_still_drops_unset_v11_fields() -> None:
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        summary="legacy evidence payload",
    )

    payload = item.model_dump(mode="json")

    assert "scope" not in payload
    assert "provenance" not in payload
    assert "runtime_run_id" not in payload
    assert EvidenceItem.model_validate(payload) == item


def test_memory_verification_status_requires_provenance_when_verified() -> None:
    base = {
        "service": "checkout-service",
        "environment": "prod",
        "memory_type": MemoryType.INVESTIGATION_SUMMARY,
        "summary": "verified incident memory",
    }
    with pytest.raises(ValidationError, match="provenance"):
        MemoryItem(
            **base,
            verification_status=MemoryVerificationStatus.VERIFIED,
        )
    with pytest.raises(ValidationError, match="provenance"):
        MemoryItem(
            **base,
            verification_status=MemoryVerificationStatus.VERIFIED,
            verified_at=datetime(2026, 8, 1, tzinfo=UTC),
            verified_by="operator",
        )

    verified = MemoryItem(
        **base,
        verification_status=MemoryVerificationStatus.VERIFIED,
        verified_at=datetime(2026, 8, 1, tzinfo=UTC),
        verified_by="operator",
        root_candidate_id="candidate-1",
        verification_evidence_ids=["ev-1"],
    )
    assert MemoryItem.model_validate(verified.model_dump(mode="json")) == verified

    unverified = MemoryItem(**base)
    assert unverified.verification_status is MemoryVerificationStatus.UNVERIFIED
    assert MemoryItem.model_validate(
        unverified.model_dump(mode="json")
    ) == unverified


def test_openai_compatible_model_provider_round_trip() -> None:
    summary = MultiAgentRunSummary(
        status=MultiAgentRunStatus.COMPLETED,
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="local-model",
        authority_mode=AuthorityMode.AGENT,
        runtime_run_id="run-openai-compatible",
    )

    payload = summary.model_dump(mode="json")
    restored = MultiAgentRunSummary.model_validate(payload)

    assert payload["model_provider"] == "openai_compatible"
    assert restored == summary
    with pytest.raises(ValidationError, match="runtime_run_id"):
        MultiAgentRunSummary(
            status=MultiAgentRunStatus.COMPLETED,
            model_provider=ModelProvider.OPENAI_COMPATIBLE,
            authority_mode=AuthorityMode.AGENT,
        )
