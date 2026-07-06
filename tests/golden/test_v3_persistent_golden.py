from backend.config.settings import AppSettings, StorageSettings
from backend.db.models import InvestigationStatus
from backend.domain.hypotheses import CauseType
from backend.services.container import AppContainer
from backend.services.incident_cases import load_incident_case


def test_v3_deployment_regression_golden_persists_through_sqlite(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'diagops-golden.db'}"
    settings = AppSettings(storage=StorageSettings(url=database_url))
    assert settings.providers.mock.enabled is True
    assert settings.providers.log_file.enabled is True
    assert settings.providers.deployment_file.enabled is True
    assert settings.providers.service_catalog.enabled is True
    assert settings.providers.prometheus.enabled is False

    container = AppContainer(settings=settings)
    record = container.orchestrator.run(load_incident_case("deployment_regression"))
    container.repository.engine.dispose()

    fresh_container = AppContainer(settings=settings)
    stored = fresh_container.repository.get(record.id)

    assert stored.status == InvestigationStatus.COMPLETED
    assert stored.report is not None
    assert stored.actions
    assert stored.verification_suggestions
    assert stored.provider_results
    assert stored.specialist_results
    assert stored.evidence
    assert stored.hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert stored.hypotheses[0].supporting_evidence_ids
    assert stored.report.investigation_id == stored.id
    assert stored.report.markdown == record.report.markdown
    assert stored.report.action_ids == [action.id for action in stored.actions]
    assert stored.report.verification_suggestion_ids == [
        suggestion.id for suggestion in stored.verification_suggestions
    ]
    assert {item.id for item in stored.evidence} >= set(
        stored.hypotheses[0].supporting_evidence_ids
    )
