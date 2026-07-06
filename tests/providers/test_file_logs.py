from datetime import UTC, datetime
from pathlib import Path

from backend.config.settings import (
    AppSettings,
    DeploymentFileProviderSettings,
    LogFileProviderSettings,
    ProviderSettings,
    ProviderToggle,
    ServiceCatalogProviderSettings,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.providers.file_logs import FileLogProvider
from backend.providers.registry import ProviderRegistry, build_provider_registry_from_settings
from backend.providers.results import ProviderStatus


def test_error_patterns_become_log_pattern_evidence():
    provider = FileLogProvider([Path("data/sample-logs/checkout-service.log")])

    result = provider.collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.LOG
    assert len(result.evidence_items) == 1
    evidence = result.evidence_items[0]
    assert evidence.kind == EvidenceKind.LOG_PATTERN
    assert evidence.payload["error_count"] == 2
    assert "NullPointerException" in "\n".join(evidence.payload["sample_lines"])
    assert "HTTP 500" in evidence.payload["patterns"]
    assert "Exception" in evidence.payload["patterns"]


def test_payload_contains_error_count_sample_lines_and_patterns():
    provider = FileLogProvider([Path("data/sample-logs/checkout-service.log")])

    evidence = provider.collect(_event()).evidence_items[0]

    assert evidence.payload["error_count"] == 2
    assert len(evidence.payload["sample_lines"]) == 2
    assert set(evidence.payload["patterns"]) >= {"ERROR", "Exception", "HTTP 500", "5xx"}
    assert evidence.payload["path"] == "data\\sample-logs\\checkout-service.log" or (
        evidence.payload["path"] == "data/sample-logs/checkout-service.log"
    )
    assert evidence.payload["service"] == "checkout-service"
    assert evidence.payload["environment"] == "prod"


def test_missing_log_file_produces_provider_failure_through_registry(tmp_path):
    registry = ProviderRegistry([FileLogProvider([tmp_path / "missing.log"])])

    evidence = registry.collect_all(_event())

    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.PROVIDER_ERROR
    assert evidence[0].provider == EvidenceProvider.LOG
    assert evidence[0].status == EvidenceStatus.FAILED
    assert evidence[0].error_message is not None


def test_log_provider_filters_service_and_timestamped_lines(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        """
2026-07-06T08:01:00+00:00 checkout-service prod ERROR HTTP 500 included
2026-07-06T09:30:00+00:00 checkout-service prod ERROR HTTP 500 outside window
2026-07-06T08:02:00+00:00 inventory-service prod ERROR HTTP 500 wrong service
""".lstrip(),
        encoding="utf-8",
    )
    provider = FileLogProvider([log_path])

    result = provider.collect(_event())

    assert len(result.evidence_items) == 1
    sample_lines = result.evidence_items[0].payload["sample_lines"]
    assert sample_lines == [
        "2026-07-06T08:01:00+00:00 checkout-service prod ERROR HTTP 500 included"
    ]


def test_log_provider_returns_success_with_empty_evidence_when_no_matches(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        "2026-07-06T08:01:00+00:00 checkout-service prod INFO ok\n",
        encoding="utf-8",
    )
    provider = FileLogProvider([log_path])

    result = provider.collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []


def test_registry_builder_adds_file_log_provider(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        "2026-07-06T08:01:00+00:00 checkout-service prod ERROR HTTP 500\n",
        encoding="utf-8",
    )
    settings = AppSettings(
        providers=ProviderSettings(
            mock=ProviderToggle(enabled=False),
            log_file=LogFileProviderSettings(enabled=True, paths=[log_path]),
            deployment_file=DeploymentFileProviderSettings(enabled=False),
            service_catalog=ServiceCatalogProviderSettings(enabled=False),
        )
    )

    registry = build_provider_registry_from_settings(settings)
    results = registry.collect_results(_event())

    assert any(isinstance(provider, FileLogProvider) for provider in registry.providers)
    assert len(results) == 1
    assert results[0].provider == EvidenceProvider.LOG
    assert results[0].evidence_items[0].payload["error_count"] == 1


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="checkout-service has elevated errors",
        started_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        time_window_minutes=30,
    )
