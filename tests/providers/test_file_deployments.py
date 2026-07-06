from datetime import UTC, datetime
from pathlib import Path

from backend.config.settings import (
    AppSettings,
    DeploymentFileProviderSettings,
    ProviderSettings,
    ProviderToggle,
    ServiceCatalogProviderSettings,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.providers.file_deployments import FileDeploymentProvider
from backend.providers.registry import ProviderRegistry, build_provider_registry_from_settings
from backend.providers.results import ProviderStatus
from backend.services.incident_cases import load_incident_case


def test_file_deployment_provider_matches_payment_service_release():
    provider = FileDeploymentProvider(Path("data/deployments/deployments.json"))
    event = load_incident_case("deployment_regression")

    result = provider.collect(event)

    assert result.status == ProviderStatus.SUCCESS
    assert result.provider == EvidenceProvider.DEPLOY
    assert len(result.evidence_items) == 1
    evidence = result.evidence_items[0]
    assert evidence.kind == EvidenceKind.DEPLOYMENT
    assert evidence.timestamp.isoformat() == "2026-07-03T14:00:00+08:00"
    assert evidence.payload["service"] == "payment-service"
    assert evidence.payload["environment"] == "prod"
    assert evidence.payload["version"] == "v1.8.2"
    assert evidence.payload["deployed_at"] == "2026-07-03T14:00:00+08:00"
    assert evidence.payload["operator"] == "release-bot"
    assert evidence.payload["commit"] == "abc1234"
    assert "v1.8.2" in evidence.payload["summary"]


def test_file_deployment_provider_ignores_out_of_window_deployment(tmp_path):
    deployments_path = tmp_path / "deployments.json"
    deployments_path.write_text(
        """
[
  {
    "service": "payment-service",
    "environment": "prod",
    "version": "v1.8.0",
    "deployed_at": "2026-07-03T09:00:00+08:00",
    "operator": "release-bot",
    "commit": "old1234",
    "summary": "old payment-service deployment"
  }
]
""".lstrip(),
        encoding="utf-8",
    )
    provider = FileDeploymentProvider(deployments_path)

    result = provider.collect(load_incident_case("deployment_regression"))

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []


def test_file_deployment_provider_filters_service_and_environment(tmp_path):
    deployments_path = tmp_path / "deployments.json"
    deployments_path.write_text(
        """
[
  {
    "service": "checkout-service",
    "environment": "prod",
    "version": "v3.4.0",
    "deployed_at": "2026-07-03T14:00:00+08:00",
    "operator": "checkout-team",
    "commit": "987abcd",
    "summary": "different service"
  },
  {
    "service": "payment-service",
    "environment": "staging",
    "version": "v1.8.2",
    "deployed_at": "2026-07-03T14:00:00+08:00",
    "operator": "release-bot",
    "commit": "abc1234",
    "summary": "different environment"
  }
]
""".lstrip(),
        encoding="utf-8",
    )
    provider = FileDeploymentProvider(deployments_path)

    result = provider.collect(load_incident_case("deployment_regression"))

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []


def test_missing_deployment_file_becomes_provider_error_evidence(tmp_path):
    registry = ProviderRegistry([FileDeploymentProvider(tmp_path / "missing.json")])

    evidence = registry.collect_all(load_incident_case("deployment_regression"))

    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.PROVIDER_ERROR
    assert evidence[0].provider == EvidenceProvider.DEPLOY
    assert evidence[0].status == EvidenceStatus.FAILED
    assert evidence[0].error_message is not None


def test_build_provider_registry_from_settings_adds_deployment_provider(tmp_path):
    deployments_path = tmp_path / "deployments.json"
    deployments_path.write_text(
        """
[
  {
    "service": "payment-service",
    "environment": "prod",
    "version": "v1.8.2",
    "deployed_at": "2026-07-03T14:00:00+08:00",
    "operator": "release-bot",
    "commit": "abc1234",
    "summary": "payment-service v1.8.2 deployment"
  }
]
""".lstrip(),
        encoding="utf-8",
    )
    settings = AppSettings(
        providers=ProviderSettings(
            mock=ProviderToggle(enabled=False),
            service_catalog=ServiceCatalogProviderSettings(enabled=False),
            deployment_file=DeploymentFileProviderSettings(
                enabled=True,
                path=deployments_path,
            ),
        )
    )

    registry = build_provider_registry_from_settings(settings)
    results = registry.collect_results(load_incident_case("deployment_regression"))

    assert len(results) == 1
    assert results[0].provider == EvidenceProvider.DEPLOY
    assert results[0].evidence_items[0].payload["version"] == "v1.8.2"


def test_file_deployment_provider_accepts_manual_event_without_match():
    provider = FileDeploymentProvider(Path("data/deployments/deployments.json"))
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service="payment-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Later payment errors",
        description="payment-service errors much later than deployment",
        started_at=datetime(2026, 7, 4, 8, 0, tzinfo=UTC),
        time_window_minutes=30,
    )

    result = provider.collect(event)

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []
