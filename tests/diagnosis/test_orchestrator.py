import logging
from datetime import UTC, datetime

import pytest

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def build_v2_orchestrator(
    repository=None,
    providers=None,
    analyzer=None,
    report_generator=None,
):
    repository = repository or InMemoryInvestigationRepository()
    providers = providers or build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=analyzer or RcaAnalyzer(),
        report_generator=report_generator or ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        action_planner=ActionPlanner(),
    )


def test_orchestrator_creates_investigation_with_report():
    repository = InMemoryInvestigationRepository()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=build_mock_provider_registry(),
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
    )

    investigation = orchestrator.run(load_incident_case("deployment_regression"))

    stored = repository.get(investigation.id)
    assert stored.id == investigation.id
    assert stored.report is not None
    assert stored.hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert "最可能根因" in stored.report.markdown


def test_orchestrator_persists_completed_record_with_actions_and_verifications():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert saved.status == InvestigationStatus.COMPLETED
    assert saved.evidence
    assert saved.hypotheses
    assert saved.report is not None
    assert saved.actions
    assert saved.verification_suggestions
    assert saved.completed_at is not None


def test_orchestrator_persists_failed_record_when_report_generation_fails():
    class BrokenReportGenerator:
        def generate(self, *args, **kwargs):
            raise RuntimeError("markdown exploded")

    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(
        repository=repository,
        report_generator=BrokenReportGenerator(),
    )
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert record.status == InvestigationStatus.FAILED
    assert saved.status == InvestigationStatus.FAILED
    assert saved.failure_reason == "markdown exploded"
    assert saved.evidence
    assert saved.hypotheses


def test_orchestrator_preserves_context_when_action_planner_fails():
    class BrokenActionPlanner:
        def plan(self, *args, **kwargs):
            raise RuntimeError("planner exploded")

    repository = InMemoryInvestigationRepository()
    providers = build_mock_provider_registry()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        action_planner=BrokenActionPlanner(),
    )
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert saved.status == InvestigationStatus.FAILED
    assert saved.failure_reason == "planner exploded"
    assert saved.evidence
    assert saved.hypotheses
    assert saved.actions == []


def test_repository_rejects_unknown_investigation_id():
    repository = InMemoryInvestigationRepository()

    with pytest.raises(ValueError, match="Unknown investigation: inv-missing"):
        repository.get("inv-missing")


def test_repository_lists_records_by_created_at_descending():
    event = load_incident_case("deployment_regression")
    repository = InMemoryInvestigationRepository()
    older = InvestigationRecord(
        id="inv-older",
        event=event,
        status=InvestigationStatus.COMPLETED,
        created_at=datetime(2026, 7, 3, 8, 0, tzinfo=UTC),
    )
    newer = InvestigationRecord(
        id="inv-newer",
        event=event,
        status=InvestigationStatus.COMPLETED,
        created_at=datetime(2026, 7, 3, 9, 0, tzinfo=UTC),
    )

    repository.save(older)
    repository.save(newer)

    assert repository.list() == [newer, older]


def test_repository_updates_status_and_failure_reason():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    record = repository.save(InvestigationRecord(event=event))

    updated = repository.update_status(
        record.id,
        InvestigationStatus.FAILED,
        failure_reason="report generation failed",
    )

    assert updated.status == InvestigationStatus.FAILED
    assert updated.failure_reason == "report generation failed"
    assert updated.updated_at >= record.created_at


def test_repository_updates_action_and_verification_status():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Deployment regression likely.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-1"],
    )
    verification = VerificationSuggestion(
        title="Check 5xx",
        description="Confirm error rate recovery.",
        expected_signal="5xx below 1%",
    )
    record = repository.save(
        InvestigationRecord(
            event=event,
            actions=[action],
            verification_suggestions=[verification],
        )
    )

    updated_action = repository.update_action_status(
        record.id,
        action.id,
        status="approved",
        note="owner approved",
    )
    updated_verification = repository.update_verification_status(
        record.id,
        verification.id,
        status="passed",
        result_note="5xx is normal",
    )

    assert updated_action.status == "approved"
    assert updated_action.note == "owner approved"
    assert updated_verification.status == "passed"
    assert updated_verification.result_note == "5xx is normal"


def test_orchestrator_logs_investigation_lifecycle(caplog):
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    with caplog.at_level(logging.INFO):
        record = orchestrator.run(event)

    messages = "\n".join(item.message for item in caplog.records)
    assert record.id in messages
    assert "investigation started" in messages
    assert "investigation completed" in messages
