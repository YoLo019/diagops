from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import select

from backend.db.models import InvestigationRecord
from backend.db.schema import evidence_items, verification_suggestions
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.human_transitions import HumanStateConflict
from backend.services.incident_cases import load_incident_case


def test_sqlite_engine_configures_busy_timeout(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'busy-timeout.db'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 10_000
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("initial", "targets"),
    [
        (ActionStatus.PROPOSED, (ActionStatus.APPROVED, ActionStatus.REJECTED)),
        (ActionStatus.APPROVED, (ActionStatus.DONE, ActionStatus.SKIPPED)),
    ],
)
def test_sqlite_concurrent_action_transition_has_one_winner(
    tmp_path, initial: ActionStatus, targets: tuple[ActionStatus, ActionStatus]
) -> None:
    database_url = f"sqlite:///{tmp_path / 'concurrency.db'}"
    setup_engine = create_db_engine(database_url)
    initialize_database(setup_engine)
    setup_repository = SQLiteInvestigationRepository(setup_engine)
    record = _record(initial)
    setup_repository.save(record)
    before = _unrelated_payloads(setup_engine, record.id)
    setup_engine.dispose()

    barrier = Barrier(2)

    def update_status(target: ActionStatus):
        engine = create_db_engine(database_url)
        repository = SQLiteInvestigationRepository(engine)
        barrier.wait()
        try:
            return repository.update_action_status(
                record.id,
                record.actions[0].id,
                status=target,
                note="owner approved" if target == ActionStatus.APPROVED else None,
            )
        except HumanStateConflict as exc:
            return exc
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update_status, targets))

    assert sum(isinstance(item, RecommendedAction) for item in outcomes) == 1
    assert sum(isinstance(item, HumanStateConflict) for item in outcomes) == 1

    final_engine = create_db_engine(database_url)
    final_repository = SQLiteInvestigationRepository(final_engine)
    assert final_repository.get(record.id).actions[0].status in set(targets)
    assert _unrelated_payloads(final_engine, record.id) == before
    final_engine.dispose()


def _unrelated_payloads(engine, investigation_id: str):
    with engine.connect() as connection:
        evidence = connection.execute(
            select(evidence_items.c.payload).where(
                evidence_items.c.investigation_id == investigation_id
            )
        ).scalars().all()
        verifications = connection.execute(
            select(verification_suggestions.c.payload).where(
                verification_suggestions.c.investigation_id == investigation_id
            )
        ).scalars().all()
    return evidence, verifications


def _record(status: ActionStatus) -> InvestigationRecord:
    event = load_incident_case("deployment_regression")
    evidence = EvidenceItem(
        id="ev-concurrency",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime(2026, 7, 13, tzinfo=UTC),
        summary="deployment evidence",
    )
    action = RecommendedAction(
        id="act-concurrency",
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Record operator decision",
        description="State only; DiagOps does not execute it.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=[evidence.id],
        status=status,
        note="owner approved" if status == ActionStatus.APPROVED else None,
    )
    return InvestigationRecord(
        id="inv-concurrency",
        event=event,
        evidence=[evidence],
        actions=[action],
        verification_suggestions=[
            VerificationSuggestion(
                id="ver-concurrency",
                title="Verify result",
                description="Record observed state.",
                expected_signal="signal changes",
            )
        ],
    )
