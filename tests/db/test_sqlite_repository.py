from datetime import UTC, datetime

from sqlalchemy import inspect, select

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.schema import schema_version
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.context import SpecialistResult, SpecialistStatus
from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
)
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.results import ProviderResult, ProviderStatus
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def sqlite_url(tmp_path):
    return f"sqlite:///{tmp_path / 'diagops-test.db'}"


def build_repository(tmp_path):
    engine = create_db_engine(sqlite_url(tmp_path))
    initialize_database(engine)
    return SQLiteInvestigationRepository(engine), engine


def completed_record() -> InvestigationRecord:
    event = load_incident_case("deployment_regression")
    evidence = [
        EvidenceItem(
            provider=EvidenceProvider.DEPLOY,
            kind=EvidenceKind.DEPLOYMENT,
            timestamp=datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
            summary="Deployment happened before the incident.",
            payload={"version": "v2"},
            confidence=0.9,
        )
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.DEPLOYMENT_REGRESSION,
            summary="Deployment regression is likely.",
            confidence=0.91,
            supporting_evidence_ids=[evidence[0].id],
        )
    ]
    actions, verifications = ActionPlanner().plan(event, evidence, hypotheses)
    report = ReportGenerator().generate(
        "inv-sqlite",
        event,
        evidence,
        hypotheses,
        actions=actions,
        verification_suggestions=verifications,
    )
    return InvestigationRecord(
        id="inv-sqlite",
        event=event,
        status=InvestigationStatus.COMPLETED,
        evidence=evidence,
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.DEPLOY,
                status=ProviderStatus.SUCCESS,
                evidence_items=evidence,
                duration_ms=12,
            )
        ],
        specialist_results=[
            SpecialistResult(
                agent_name="DeployAnalyst",
                status=SpecialistStatus.COMPLETED,
                evidence_items=evidence,
                summary="deploy provider returned 1 evidence item(s)",
                duration_ms=12,
            )
        ],
        hypotheses=hypotheses,
        report=report,
        actions=actions,
        verification_suggestions=verifications,
        created_at=datetime(2026, 7, 3, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 3, 10, 5, tzinfo=UTC),
        completed_at=datetime(2026, 7, 3, 10, 5, tzinfo=UTC),
    )


def test_schema_initialization_creates_schema_version(tmp_path):
    engine = create_db_engine(sqlite_url(tmp_path))
    initialize_database(engine)

    tables = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        version = connection.execute(select(schema_version.c.version)).scalar_one()

    assert "schema_version" in tables
    assert version == 3


def test_save_get_list_round_trips_completed_investigation(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()

    repository.save(record)

    stored = repository.get(record.id)
    assert stored == record
    assert repository.list() == [record]


def test_round_trips_provider_and_specialist_results(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()

    repository.save(record)

    stored = repository.get(record.id)
    assert stored.provider_results == record.provider_results
    assert stored.specialist_results == record.specialist_results
    assert stored.provider_results[0].evidence_items == record.evidence


def test_failed_provider_results_are_persisted(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = InvestigationRecord(
        event=load_incident_case("deployment_regression"),
        status=InvestigationStatus.FAILED,
        failure_reason="provider timeout",
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.LOG,
                status=ProviderStatus.FAILED,
                error_message="log file missing",
                duration_ms=7,
            )
        ],
        specialist_results=[
            SpecialistResult(
                agent_name="LogAnalyst",
                status=SpecialistStatus.FAILED,
                summary="log provider returned 0 evidence item(s)",
                errors=["log file missing"],
                duration_ms=7,
            )
        ],
    )

    repository.save(record)

    stored = repository.get(record.id)
    assert stored.provider_results == record.provider_results
    assert stored.specialist_results == record.specialist_results


def test_new_repository_instance_reads_same_sqlite_file(tmp_path):
    database_url = sqlite_url(tmp_path)
    engine = create_db_engine(database_url)
    initialize_database(engine)
    SQLiteInvestigationRepository(engine).save(completed_record())

    new_engine = create_db_engine(database_url)
    initialize_database(new_engine)
    stored = SQLiteInvestigationRepository(new_engine).get("inv-sqlite")

    assert stored.report is not None
    assert stored.actions
    assert stored.verification_suggestions


def test_update_action_status_persists_status_and_note(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()
    repository.save(record)

    updated = repository.update_action_status(
        record.id,
        record.actions[0].id,
        status=ActionStatus.APPROVED,
        note="owner approved",
    )

    stored = repository.get(record.id)
    assert updated.status == ActionStatus.APPROVED
    assert updated.note == "owner approved"
    assert stored.actions[0].status == ActionStatus.APPROVED
    assert stored.actions[0].note == "owner approved"


def test_update_verification_status_persists_status_and_result_note(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()
    repository.save(record)

    updated = repository.update_verification_status(
        record.id,
        record.verification_suggestions[0].id,
        status=VerificationStatus.PASSED,
        result_note="5xx recovered",
    )

    stored = repository.get(record.id)
    assert updated.status == VerificationStatus.PASSED
    assert updated.result_note == "5xx recovered"
    assert stored.verification_suggestions[0].status == VerificationStatus.PASSED
    assert stored.verification_suggestions[0].result_note == "5xx recovered"


def test_failed_investigations_persist_failure_reason(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = InvestigationRecord(
        event=load_incident_case("deployment_regression"),
        status=InvestigationStatus.FAILED,
        failure_reason="provider timeout",
    )

    repository.save(record)

    stored = repository.get(record.id)
    assert stored.status == InvestigationStatus.FAILED
    assert stored.failure_reason == "provider timeout"


def test_list_orders_by_created_at_descending(tmp_path):
    repository, _engine = build_repository(tmp_path)
    older = completed_record()
    older.id = "inv-older"
    older.created_at = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)
    newer = completed_record()
    newer.id = "inv-newer"
    newer.created_at = datetime(2026, 7, 3, 9, 0, tzinfo=UTC)

    repository.save(older)
    repository.save(newer)

    assert [record.id for record in repository.list()] == ["inv-newer", "inv-older"]


def test_update_status_sets_completed_at_like_memory_repository(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = repository.save(
        InvestigationRecord(event=load_incident_case("deployment_regression"))
    )

    updated = repository.update_status(record.id, InvestigationStatus.COMPLETED)

    assert updated.completed_at == updated.updated_at


def test_status_updates_preserve_exception_messages(tmp_path):
    repository, _engine = build_repository(tmp_path)
    action = RecommendedAction(
        action_type=ActionType.CHECK,
        title="Check deploy",
        description="Review deployment metadata.",
        risk_level=ActionRiskLevel.READ_ONLY,
        requires_approval=False,
        supporting_evidence_ids=["ev-1"],
    )
    verification = VerificationSuggestion(
        title="Check recovery",
        description="Look at 5xx rate.",
        expected_signal="5xx below threshold",
    )
    record = InvestigationRecord(
        event=load_incident_case("deployment_regression"),
        actions=[action],
        verification_suggestions=[verification],
    )
    repository.save(record)

    try:
        repository.get("inv-missing")
    except ValueError as exc:
        assert str(exc) == "Unknown investigation: inv-missing"
    try:
        repository.update_action_status(record.id, "act-missing", status="approved")
    except ValueError as exc:
        assert str(exc) == "Unknown action: act-missing"
    try:
        repository.update_verification_status(record.id, "ver-missing", status="passed")
    except ValueError as exc:
        assert str(exc) == "Unknown verification suggestion: ver-missing"
