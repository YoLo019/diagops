import pytest
from pydantic import ValidationError

from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.providers.results import ProviderResult, ProviderStatus


def test_provider_result_defaults_to_success():
    result = ProviderResult(provider=EvidenceProvider.LOG)

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []
    assert result.duration_ms >= 0


def test_provider_result_rejects_negative_duration():
    with pytest.raises(ValidationError):
        ProviderResult(provider=EvidenceProvider.LOG, duration_ms=-1)


def test_failed_provider_result_creates_error_evidence():
    result = ProviderResult(
        provider=EvidenceProvider.LOG,
        status=ProviderStatus.FAILED,
        error_message="loki timeout",
        duration_ms=25,
    )

    evidence = result.to_error_evidence()

    assert evidence.provider == EvidenceProvider.LOG
    assert evidence.kind == EvidenceKind.PROVIDER_ERROR
    assert evidence.status == EvidenceStatus.FAILED
    assert evidence.error_message == "loki timeout"
    assert evidence.payload["provider_status"] == "failed"
