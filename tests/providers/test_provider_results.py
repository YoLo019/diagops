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


def test_registry_bounds_results_after_redaction_and_preserves_failed_status():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from backend.domain.evidence import EvidenceItem
    from backend.providers.registry import ProviderRegistry
    from tests.reports.test_v11_product_integration import _event

    class Provider:
        provider = EvidenceProvider.LOG
        supported_tools = ("read_logs",)
        status = ProviderStatus.SUCCESS

        def collect(self, event, query=None):
            return ProviderResult(
                provider=self.provider,
                status=self.status,
                error_message="provider failure" if self.status == ProviderStatus.FAILED else None,
                evidence_items=[
                    EvidenceItem(
                        id=f"ev-{index}",
                        provider=self.provider,
                        kind=EvidenceKind.LOG_PATTERN,
                        timestamp=datetime.now(UTC),
                        summary="observed " * 100,
                        payload={"details": "x" * (17000 if index == 2 else 10)},
                    )
                    for index in range(3)
                ],
            )

    provider = Provider()
    registry = ProviderRegistry([provider])
    query = registry.query_results(_event(), "read_logs", SimpleNamespace(limit=2))[0]
    assert query.truncated and query.returned_count == 2
    assert query.status == ProviderStatus.PARTIAL
    assert all(len(item.summary) == 512 for item in query.evidence_items)
    initial = registry.collect_results(_event())[0]
    assert initial.truncated and initial.returned_count == 2
    provider.status = ProviderStatus.FAILED
    failed = registry.query_results(_event(), "read_logs", SimpleNamespace(limit=1))[0]
    assert failed.status == ProviderStatus.FAILED
    assert failed.error_message == "provider failure"
