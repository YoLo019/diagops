from fastapi import APIRouter, HTTPException

from backend.db.models import InvestigationRecord, InvestigationSummary
from backend.domain.events import IncidentEvent
from backend.domain.multi_agent import InvestigationStrategy
from backend.services.container import get_container
from backend.services.incident_cases import list_case_ids, load_incident_case

router = APIRouter(prefix="/events", tags=["events"])


def to_summary(
    record: InvestigationRecord,
    *,
    runtime_available: bool | None = None,
) -> InvestigationSummary:
    summary = InvestigationSummary.from_record(record)
    if runtime_available is None:
        return summary
    return summary.model_copy(update={"runtime_available": runtime_available})


@router.post("", response_model=InvestigationSummary)
async def create_event(
    event: IncidentEvent,
    strategy: InvestigationStrategy | None = None,
) -> InvestigationSummary:
    container = get_container()
    record = await container.run_investigation(event, strategy=strategy)
    return to_summary(
        record,
        runtime_available=bool(container.runtime_store.list_runs(record.id)),
    )


@router.post("/simulated/{case_id}", response_model=InvestigationSummary)
async def create_simulated_event(case_id: str) -> InvestigationSummary:
    if case_id not in list_case_ids():
        raise HTTPException(status_code=404, detail=f"Unknown incident case: {case_id}")

    try:
        event = load_incident_case(case_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to load simulated incident case"
        ) from exc

    container = get_container()
    record = await container.run_investigation(event)
    return to_summary(
        record,
        runtime_available=bool(container.runtime_store.list_runs(record.id)),
    )
