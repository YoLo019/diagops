from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.models import InvestigationRecord
from backend.domain.events import IncidentEvent
from backend.services.container import get_container
from backend.services.incident_cases import list_case_ids, load_incident_case

router = APIRouter(prefix="/events", tags=["events"])


class InvestigationSummary(BaseModel):
    id: str
    status: str
    service: str
    title: str
    top_cause_type: str
    confidence: float
    action_count: int = 0
    verification_count: int = 0
    failure_reason: str | None = None


def to_summary(record: InvestigationRecord) -> InvestigationSummary:
    top = record.hypotheses[0] if record.hypotheses else None
    return InvestigationSummary(
        id=record.id,
        status=record.status,
        service=record.event.service,
        title=record.event.title,
        top_cause_type=top.cause_type if top else "unknown",
        confidence=top.confidence if top else 0.0,
        action_count=len(record.actions),
        verification_count=len(record.verification_suggestions),
        failure_reason=record.failure_reason,
    )


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
