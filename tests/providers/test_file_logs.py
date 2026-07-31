from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from backend.domain.tool_queries import LogQuery
from backend.providers.file_logs import FileLogProvider
from backend.providers.registry import ProviderRegistry, build_provider_registry_from_settings
from backend.providers.results import ProviderStatus


def test_error_patterns_become_log_pattern_evidence():
    provider = FileLogProvider([Path("data/sample-logs/checkout-service.log")])

    result = provider.collect(_event())

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.LOG
    assert len(result.evidence_items) == 2
    assert all(item.kind == EvidenceKind.LOG_PATTERN for item in result.evidence_items)
    assert sum(item.payload["error_count"] for item in result.evidence_items) == 2
    sample_lines = [
        line
        for item in result.evidence_items
        for line in item.payload["sample_lines"]
    ]
    patterns = {
        pattern
        for item in result.evidence_items
        for pattern in item.payload["patterns"]
    }
    assert "NullPointerException" in "\n".join(sample_lines)
    assert {"HTTP 500", "Exception"} <= patterns


def test_payload_contains_error_count_sample_lines_and_patterns():
    provider = FileLogProvider([Path("data/sample-logs/checkout-service.log")])

    evidence = provider.collect(_event()).evidence_items

    assert sum(item.payload["error_count"] for item in evidence) == 2
    assert sum(len(item.payload["sample_lines"]) for item in evidence) == 2
    assert {
        pattern for item in evidence for pattern in item.payload["patterns"]
    } >= {"ERROR", "Exception", "HTTP 500", "5xx"}
    first = evidence[0]
    assert first.payload["source"] == "configured_log_file"
    assert "path" not in first.payload
    assert first.payload["service"] == "checkout-service"
    assert first.payload["environment"] == "prod"
    assert first.payload["component"] == "checkout-service"
    assert first.payload["signal_type"] == "error"


def test_structured_log_fields_become_canonical_timeout_evidence(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        "2026-07-06T08:01:00+00:00 service=checkout-service environment=prod "
        "component=checkout-api dependency=payment-service "
        "exception=TimeoutException level=ERROR\n",
        encoding="utf-8",
    )

    evidence = FileLogProvider([log_path]).collect(_event()).evidence_items[0]

    assert evidence.payload["component"] == "checkout-api"
    assert evidence.payload["dependency"] == "payment-service"
    assert evidence.payload["exception"] == "TimeoutException"
    assert evidence.payload["signal_type"] == "timeout"


def test_missing_log_file_produces_provider_failure_through_registry(tmp_path):
    registry = ProviderRegistry([FileLogProvider([tmp_path / "missing.log"])])

    evidence = registry.collect_all(_event())

    failed = next(item for item in evidence if item.provider == EvidenceProvider.LOG)
    assert failed.kind == EvidenceKind.PROVIDER_ERROR
    assert failed.status == EvidenceStatus.FAILED
    assert failed.error_message is not None


def test_log_provider_filters_service_and_timestamped_lines(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        """
2026-07-06T08:01:00+00:00 checkout-service prod ERROR HTTP 500 included
2026-07-06T09:30:00+00:00 checkout-service prod ERROR HTTP 500 outside window
2026-07-06T08:02:00+00:00 inventory-service prod ERROR HTTP 500 wrong service
2026-07-06T08:03:00+00:00 checkout-service staging ERROR HTTP 500 wrong environment
checkout-service prod ERROR HTTP 500 missing timestamp
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


def test_log_query_filters_keyword_instance_and_limit(tmp_path):
    log_path = tmp_path / "checkout.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-07-06T08:01:00+00:00 checkout-service prod pod-1 ERROR timeout first",
                "2026-07-06T08:02:00+00:00 checkout-service prod pod-2 ERROR ignored other",
                "2026-07-06T08:03:00+00:00 checkout-service prod pod-2 ERROR timeout selected",
                "2026-07-06T08:04:00+00:00 checkout-service prod pod-2 ERROR timeout limited",
            ]
        ),
        encoding="utf-8",
    )
    event = _event()

    result = FileLogProvider([log_path]).collect(
        event,
        LogQuery(
            start_time=event.started_at,
            end_time=event.started_at + timedelta(minutes=10),
            reason="定位 pod-2 超时",
            keywords=["timeout"],
            levels=["ERROR"],
            instance="pod-2",
            limit=1,
        ),
    )

    assert result.evidence_items[0].payload["error_count"] == 1
    assert result.evidence_items[0].payload["sample_lines"] == [
        "2026-07-06T08:03:00+00:00 checkout-service prod pod-2 ERROR timeout selected"
    ]


def test_log_query_limit_applies_across_configured_files(tmp_path):
    paths = [tmp_path / "one.log", tmp_path / "two.log"]
    for index, path in enumerate(paths, start=1):
        path.write_text(
            f"2026-07-06T08:0{index}:00+00:00 checkout-service prod ERROR timeout\n",
            encoding="utf-8",
        )
    event = _event()
    query = LogQuery(
        start_time=event.started_at,
        end_time=event.started_at + timedelta(minutes=10),
        reason="限制跨文件返回量",
        keywords=["timeout"],
        limit=1,
    )

    result = FileLogProvider(paths).collect(event, query)

    assert sum(item.payload["error_count"] for item in result.evidence_items) == 1


def test_log_provider_rejects_oversized_input(tmp_path):
    log_path = tmp_path / "large.log"
    log_path.write_text("x" * 65, encoding="utf-8")

    provider = FileLogProvider([log_path], max_bytes=64)

    with pytest.raises(ValueError, match="size limit"):
        provider.collect(_event())


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
    log_result = next(
        result for result in results if result.provider == EvidenceProvider.LOG
    )
    assert log_result.evidence_items[0].payload["error_count"] == 1
    assert sum(result.status == ProviderStatus.SKIPPED for result in results) == 5


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
