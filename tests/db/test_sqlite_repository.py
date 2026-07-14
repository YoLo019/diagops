from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, update

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.schema import evidence_items, llm_analyses, schema_version
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
from backend.domain.llm_analysis import LLMAnalysis
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
    assert version == 5


def test_aggregate_save_updates_parent_in_place_and_preserves_legacy_rows(tmp_path):
    repository, engine = build_repository(tmp_path)
    record = completed_record()
    repository.save(record)
    analysis = LLMAnalysis.create(
        investigation_id=record.id,
        existing_evidence_ids={record.evidence[0].id},
        summary="Historical analysis.",
        referenced_evidence_ids=[record.evidence[0].id],
    )
    with engine.begin() as connection:
        connection.execute(
            llm_analyses.insert().values(
                investigation_id=record.id,
                payload=analysis.model_dump(mode="json"),
            )
        )
    with engine.connect() as connection:
        before_rowid = connection.exec_driver_sql(
            "SELECT rowid FROM investigations WHERE id = ?", (record.id,)
        ).scalar_one()
        before_llm = connection.execute(
            select(llm_analyses.c.payload).where(
                llm_analyses.c.investigation_id == record.id
            )
        ).scalar_one()

    record.event = record.event.model_copy(update={"title": "updated title"})
    repository.save(record)

    with engine.connect() as connection:
        after_rowid = connection.exec_driver_sql(
            "SELECT rowid FROM investigations WHERE id = ?", (record.id,)
        ).scalar_one()
        after_llm = connection.execute(
            select(llm_analyses.c.payload).where(
                llm_analyses.c.investigation_id == record.id
            )
        ).scalar_one()
    assert after_rowid == before_rowid
    assert after_llm == before_llm


def test_aggregate_save_rolls_back_parent_and_children_on_replacement_failure(
    tmp_path, monkeypatch
):
    repository, _engine = build_repository(tmp_path)
    original = completed_record()
    repository.save(original)
    expected = repository.get(original.id)
    replacement = expected.model_copy(deep=True)
    replacement.event.title = "must roll back"
    replacement.evidence[0].summary = "must roll back child"
    original_replace = repository._replace_children
    calls = 0

    def fail_midway(*args, **kwargs):
        nonlocal calls
        calls += 1
        original_replace(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected child failure")

    monkeypatch.setattr(repository, "_replace_children", fail_midway)

    with pytest.raises(RuntimeError, match="injected child failure"):
        repository.save(replacement)

    assert repository.get(original.id) == expected


def test_save_get_list_round_trips_completed_investigation(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()

    repository.save(record)

    stored = repository.get(record.id)
    assert stored == record
    assert repository.list() == [record]


def test_summary_query_is_newest_first_and_does_not_load_detail_payloads(tmp_path):
    repository, engine = build_repository(tmp_path)
    older = completed_record()
    newer_time = datetime(2026, 7, 3, 11, 0, tzinfo=UTC)
    newer = InvestigationRecord(
        id="inv-newer",
        event=older.event,
        status=InvestigationStatus.COMPLETED,
        hypotheses=[older.hypotheses[0].model_copy(update={"id": "hyp-newer"})],
        actions=[older.actions[0].model_copy(update={"id": "act-newer"})],
        verification_suggestions=[
            older.verification_suggestions[0].model_copy(update={"id": "ver-newer"})
        ],
        created_at=newer_time,
        updated_at=newer_time,
        completed_at=newer_time,
    )
    repository.save(older)
    repository.save(newer)
    with engine.begin() as connection:
        connection.execute(
            update(evidence_items)
            .where(evidence_items.c.investigation_id == older.id)
            .values(payload={"invalid": "historical detail"})
        )

    summaries = repository.list_summaries()

    assert [summary.id for summary in summaries] == [newer.id, older.id]
    assert summaries[0].top_cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert summaries[0].action_count == len(newer.actions)
    assert summaries[0].verification_count == len(newer.verification_suggestions)


def test_round_trips_provider_and_specialist_results(tmp_path):
    repository, _engine = build_repository(tmp_path)
    record = completed_record()

    repository.save(record)

    stored = repository.get(record.id)
    assert stored.provider_results == record.provider_results
    assert stored.specialist_results == record.specialist_results
    assert stored.provider_results[0].evidence_items == record.evidence


def test_aggregate_save_does_not_write_new_llm_analysis(tmp_path):
    repository, engine = build_repository(tmp_path)
    record = completed_record()
    record.llm_analysis = LLMAnalysis.create(
        investigation_id=record.id,
        existing_evidence_ids={record.evidence[0].id},
        summary="Retired writer must be ignored.",
        referenced_evidence_ids=[record.evidence[0].id],
    )

    repository.save(record)

    with engine.connect() as connection:
        stored = connection.execute(
            select(llm_analyses).where(
                llm_analyses.c.investigation_id == record.id
            )
        ).one_or_none()
    assert stored is None


def test_new_repository_reads_directly_inserted_historical_llm_analysis(tmp_path):
    database_url = sqlite_url(tmp_path)
    engine = create_db_engine(database_url)
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    record = completed_record()
    repository.save(record)
    analysis = LLMAnalysis.create(
        investigation_id=record.id,
        existing_evidence_ids={record.evidence[0].id},
        summary="Historical analysis.",
        referenced_evidence_ids=[record.evidence[0].id],
    )
    with engine.begin() as connection:
        connection.execute(
            llm_analyses.insert().values(
                investigation_id=record.id,
                payload=analysis.model_dump(mode="json"),
            )
        )
    engine.dispose()

    restarted_engine = create_db_engine(database_url)
    initialize_database(restarted_engine)
    restarted = SQLiteInvestigationRepository(restarted_engine)

    assert restarted.get(record.id).llm_analysis == analysis


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
        result_evidence_ids=[record.evidence[0].id],
        related_action_ids=[record.actions[0].id],
    )

    stored = repository.get(record.id)
    assert updated.status == VerificationStatus.PASSED
    assert updated.result_note == "5xx recovered"
    assert updated.result_evidence_ids == [record.evidence[0].id]
    assert updated.related_action_ids == [record.actions[0].id]
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
