from fastapi import APIRouter, HTTPException

from backend.db.models import InvestigationRecord, InvestigationSummary
from backend.domain.events import IncidentEvent
from backend.services.container import get_container
from backend.services.incident_cases import list_case_ids, load_incident_case

router = APIRouter(prefix="/events", tags=["events"])


def to_summary(record: InvestigationRecord) -> InvestigationSummary:
    return InvestigationSummary.from_record(record)


@router.post("", response_model=InvestigationSummary)
def create_event(event: IncidentEvent) -> InvestigationSummary:
    container = get_container()
    record = container.orchestrator.run(event)
    return to_summary(record)


@router.post("/simulated/{case_id}", response_model=InvestigationSummary)
def create_simulated_event(case_id: str) -> InvestigationSummary:
    if case_id not in list_case_ids():
        raise HTTPException(status_code=404, detail=f"Unknown incident case: {case_id}")

    try:
        event = load_incident_case(case_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Failed to load simulated incident case"
        ) from exc

    container = get_container()
    record = container.orchestrator.run(event)
    return to_summary(record)
