from datetime import UTC, datetime

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import InMemoryRuntimeStore


@pytest.fixture(params=("memory", "sqlite"))
def runtime_store(request, tmp_path):
    if request.param == "memory":
        repository = InMemoryInvestigationRepository()
        repository.save(_investigation("inv-1"))
        repository.save(_investigation("inv-2"))
        return InMemoryRuntimeStore(repository)

    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(_investigation("inv-1"))
    repository.save(_investigation("inv-2"))
    return SQLiteRuntimeStore(engine, repository)


def _investigation(investigation_id: str) -> InvestigationRecord:
    return InvestigationRecord(
        id=investigation_id,
        event=IncidentEvent(
            source=IncidentSource.MANUAL,
            service="checkout-service",
            environment="prod",
            severity=Severity.WARNING,
            title=f"runtime {investigation_id}",
            description="runtime store contract",
            started_at=datetime(2026, 7, 17, tzinfo=UTC),
        ),
    )
