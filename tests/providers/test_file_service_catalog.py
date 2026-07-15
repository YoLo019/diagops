from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.config.settings import (
    AppSettings,
    ProviderSettings,
    ProviderToggle,
    ServiceCatalogProviderSettings,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import ServiceCatalogQuery
from backend.providers.file_service_catalog import FileServiceCatalogProvider
from backend.providers.registry import ProviderRegistry, build_provider_registry_from_settings
from backend.providers.results import ProviderStatus


def test_file_service_catalog_loads_default_config():
    provider = FileServiceCatalogProvider(Path("config/services.yaml"))
    event = _event(service="checkout-service")

    result = provider.collect(event)

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.SERVICE_CATALOG
    assert len(result.evidence_items) == 1


def test_file_service_catalog_matches_checkout_service_metadata():
    provider = FileServiceCatalogProvider(Path("config/services.yaml"))
    event = _event(service="checkout-service", environment="prod")

    result = provider.collect(event)

    evidence = result.evidence_items[0]
    assert evidence.kind == EvidenceKind.SERVICE_METADATA
    assert evidence.payload["service"] == "checkout-service"
    assert evidence.payload["owner"] == "platform-team"
    assert evidence.payload["team"] == "payments-platform"
    assert evidence.payload["runtime"] == "python"
    assert evidence.payload["repository"] == "https://example.invalid/checkout-service"
    assert evidence.payload["dependencies"] == ["inventory-service", "payment-db"]
    assert evidence.payload["dashboards"] == ["http://grafana.local/d/checkout"]
    assert evidence.payload["runbooks"] == ["http://runbooks.local/checkout"]
    assert evidence.payload["environment"] == "prod"


def test_file_service_catalog_missing_service_returns_skipped_result():
    provider = FileServiceCatalogProvider(Path("config/services.yaml"))
    event = _event(service="missing-service")

    result = provider.collect(event)

    assert result.status == ProviderStatus.SKIPPED
    assert result.evidence_items == []
    assert result.error_message == "Service not found in catalog: missing-service"


def test_file_service_catalog_format_error_becomes_failed_registry_result(tmp_path):
    catalog_path = tmp_path / "services.yaml"
    catalog_path.write_text("services:\n  - checkout-service\n", encoding="utf-8")
    registry = ProviderRegistry([FileServiceCatalogProvider(catalog_path)])

    results = registry.collect_results(_event(service="checkout-service"))

    assert results[0].status == ProviderStatus.FAILED
    assert results[0].error_message == "provider collection failed"


def test_build_provider_registry_from_settings_adds_file_service_catalog(tmp_path):
    catalog_path = tmp_path / "services.yaml"
    catalog_path.write_text(
        """
services:
  checkout-service:
    owner: catalog-team
    team: checkout
    runtime: python
    repository: https://example.invalid/repo
    dependencies: []
    dashboards: []
    runbooks: []
""".lstrip(),
        encoding="utf-8",
    )
    settings = AppSettings(
        providers=ProviderSettings(
            mock=ProviderToggle(enabled=False),
            service_catalog=ServiceCatalogProviderSettings(
                enabled=True,
                path=catalog_path,
            ),
        )
    )

    registry = build_provider_registry_from_settings(settings)
    result = registry.collect_results(_event(service="checkout-service"))[0]

    assert result.provider == EvidenceProvider.SERVICE_CATALOG
    assert result.evidence_items[0].payload["owner"] == "catalog-team"


def test_build_provider_registry_from_settings_preserves_mock_providers():
    settings = AppSettings(
        providers=ProviderSettings(
            mock=ProviderToggle(enabled=True),
            service_catalog=ServiceCatalogProviderSettings(enabled=False),
        )
    )

    registry = build_provider_registry_from_settings(settings)

    assert {provider.provider for provider in registry.simulation_providers} >= {
        EvidenceProvider.LOG,
        EvidenceProvider.METRIC,
        EvidenceProvider.DEPLOY,
        EvidenceProvider.DEPENDENCY,
        EvidenceProvider.SERVICE_CATALOG,
        EvidenceProvider.RELATED_ALERT,
    }


def test_catalog_environment_allowlist_excludes_wrong_environment(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(
        "services:\n  checkout-service:\n    environments: [staging]\n",
        encoding="utf-8",
    )

    result = FileServiceCatalogProvider(path).collect(
        _event("checkout-service", environment="prod")
    )

    assert result.status == ProviderStatus.SKIPPED
    assert result.evidence_items == []


def test_catalog_records_global_environment_scope() -> None:
    evidence = FileServiceCatalogProvider(Path("config/services.yaml")).collect(
        _event("checkout-service", environment="prod")
    ).evidence_items[0]

    assert evidence.payload["environment_scope"] == "global"
    assert evidence.payload["time_filter"] == "not_applicable"


def test_catalog_query_can_exclude_dependencies() -> None:
    event = _event("checkout-service")
    query = ServiceCatalogQuery(
        start_time=event.started_at,
        end_time=event.started_at + timedelta(minutes=1),
        reason="只读取服务元数据",
        include_dependencies=False,
    )

    evidence = FileServiceCatalogProvider(Path("config/services.yaml")).collect(
        event, query
    ).evidence_items[0]

    assert evidence.payload["dependencies"] == []


def _event(service: str, environment: str = "prod") -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service=service,
        environment=environment,
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="checkout-service has elevated errors",
        started_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
    )
