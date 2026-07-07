from datetime import UTC, datetime

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.memory import MemoryItem, MemoryType
from backend.memory import MemoryStore


def dt(minute: int) -> datetime:
    return datetime(2026, 7, 6, 9, minute, tzinfo=UTC)


def memory(
    memory_id: str,
    service: str = "checkout",
    environment: str = "prod",
    created_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        service=service,
        environment=environment,
        memory_type=MemoryType.INVESTIGATION_SUMMARY,
        summary=f"memory {memory_id}",
        created_at=created_at or dt(0),
    )


def record() -> InvestigationRecord:
    return InvestigationRecord(
        id="inv-1",
        event=IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="checkout",
            environment="prod",
            severity=Severity.WARNING,
            title="Checkout errors",
            description="5xx increased after deploy",
            started_at=dt(0),
        ),
    )


def test_save_list_filters_service_and_environment():
    store = MemoryStore(InMemoryInvestigationRepository())
    wanted = memory("mem-1")

    store.save_many(
        [
            wanted,
            memory("mem-2", environment="staging"),
            memory("mem-3", service="payments"),
        ]
    )

    assert store.list("checkout", "prod") == [wanted]


def test_list_returns_newest_first():
    store = MemoryStore(InMemoryInvestigationRepository())
    older = memory("mem-1", created_at=dt(1))
    newer = memory("mem-2", created_at=dt(2))

    store.save_many([older, newer])

    assert store.list("checkout", "prod") == [newer, older]


def test_save_replaces_same_id():
    store = MemoryStore(InMemoryInvestigationRepository())
    original = memory("mem-1", created_at=dt(1))
    updated = original.model_copy(update={"summary": "updated"})

    store.save(original)
    store.save(updated)

    assert store.list("checkout", "prod") == [updated]


def test_record_feedback_creates_human_feedback_memory():
    store = MemoryStore(InMemoryInvestigationRepository())

    item = store.record_feedback(record(), True, False, "passed", "rollback worked")

    assert item.memory_type == MemoryType.HUMAN_FEEDBACK
    assert item.service == "checkout"
    assert item.environment == "prod"
    assert item.source_investigation_id == "inv-1"
    assert "root_cause_correct=True" in item.summary
    assert "action_useful:false" in item.tags
