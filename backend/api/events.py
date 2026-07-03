from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.models import InvestigationRecord
from backend.domain.events import IncidentEvent
from backend.services.container import container
from backend.services.incident_cases import load_incident_case

router = APIRouter(prefix="/events", tags=["events"])


class InvestigationSummary(BaseModel):
    id: str
    status: str
    service: str
    title: str
    top_cause_type: str
    confidence: float


def to_summary(record: InvestigationRecord) -> InvestigationSummary:
    top = record.hypotheses[0]
    return InvestigationSummary(
        id=record.id,
        status=record.status,
        service=record.event.service,
        title=record.event.title,
        top_cause_type=top.cause_type,
        confidence=top.confidence,
    )


@router.post("", response_model=InvestigationSummary)
def create_event(event: IncidentEvent) -> InvestigationSummary:
    record = container.orchestrator.run(event)
    return to_summary(record)


@router.post("/simulated/{case_id}", response_model=InvestigationSummary)
def create_simulated_event(case_id: str) -> InvestigationSummary:
    try:
        event = load_incident_case(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record = container.orchestrator.run(event)
    return to_summary(record)
