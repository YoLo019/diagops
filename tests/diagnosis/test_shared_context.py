from datetime import UTC, datetime

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.context_store import SharedContextStore
from backend.domain.agent_context import ContextFact, ContextFactType
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


def dt(minute: int = 0) -> datetime:
    return datetime(2026, 7, 6, 9, minute, tzinfo=UTC)


def evidence(evidence_id: str = "ev-1") -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=dt(),
        summary="Errors increased after deploy.",
    )


def record(investigation_id: str = "inv-1") -> InvestigationRecord:
    return InvestigationRecord(
        id=investigation_id,
        event=IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="checkout-service",
            environment="prod",
            severity=Severity.CRITICAL,
            title="Checkout errors",
            description="Checkout 5xx rate increased.",
            started_at=dt(),
        ),
        evidence=[evidence()],
    )


def fact(
    fact_id: str = "fact-1",
    *,
    evidence_ids: list[str] | None = None,
    summary: str = "Errors increased after deploy.",
) -> ContextFact:
    return ContextFact(
        id=fact_id,
        source_agent="LogAgent",
        fact_type=ContextFactType.OBSERVATION,
        summary=summary,
        confidence=0.9,
        evidence_ids=evidence_ids or ["ev-1"],
        created_at=dt(1),
    )


def memory_store() -> SharedContextStore:
    repository = InMemoryInvestigationRepository()
    repository.save(record())
    return SharedContextStore(repository)


def test_adds_and_lists_fact():
    store = memory_store()
    context_fact = fact()

    assert store.add_fact("inv-1", context_fact) == context_fact
    assert store.list_facts("inv-1") == [context_fact]


def test_rejects_unknown_evidence_id():
    store = memory_store()

    with pytest.raises(ValueError, match="unknown evidence id"):
        store.add_fact("inv-1", fact(evidence_ids=["ev-missing"]))


def test_missing_evidence_fact_can_omit_evidence_ids():
    store = memory_store()
    context_fact = ContextFact(
        id="fact-missing",
        source_agent="MetricAgent",
        fact_type=ContextFactType.MISSING_EVIDENCE,
        summary="Need metrics from the incident window.",
        confidence=0.4,
        created_at=dt(2),
    )

    store.add_fact("inv-1", context_fact)

    assert store.list_facts("inv-1") == [context_fact]


def test_add_fact_replaces_existing_fact_by_id():
    store = memory_store()
    original = fact()
    updated = fact(summary="Errors increased after the release.")

    store.add_fact("inv-1", original)
    store.add_fact("inv-1", updated)

    assert store.list_facts("inv-1") == [updated]


def test_sqlite_repository_persists_context_facts(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'diagops-v4-test.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(record())
    context_fact = fact()

    SharedContextStore(repository).add_fact("inv-1", context_fact)

    reloaded = SharedContextStore(SQLiteInvestigationRepository(engine))
    assert reloaded.list_facts("inv-1") == [context_fact]
