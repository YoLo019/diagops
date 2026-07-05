from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.api.events import InvestigationSummary, to_summary
from backend.db.models import InvestigationRecord
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.services.container import get_container

router = APIRouter(prefix="/investigations", tags=["investigations"])


class ManualInvestigationRequest(BaseModel):
    text: str
    service: str
    environment: str


class UpdateActionStatusRequest(BaseModel):
    status: ActionStatus
    note: str | None = None


class UpdateVerificationStatusRequest(BaseModel):
    status: VerificationStatus
    result_note: str | None = None


@router.get("", response_model=list[InvestigationRecord])
def list_investigations() -> list[InvestigationRecord]:
    container = get_container()
    return container.repository.list()


@router.post("/manual", response_model=InvestigationSummary)
def create_manual_investigation(
    request: ManualInvestigationRequest,
) -> InvestigationSummary:
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service=request.service,
        environment=request.environment,
        severity=Severity.WARNING,
        title=request.text[:80],
        description=request.text,
        started_at=datetime.now(UTC),
    )
    container = get_container()
    record = container.orchestrator.run(event)
    return to_summary(record)


@router.patch("/{investigation_id}/actions/{action_id}")
def update_action_status(
    investigation_id: str,
    action_id: str,
    request: UpdateActionStatusRequest,
):
    container = get_container()
    try:
        return container.repository.update_action_status(
            investigation_id,
            action_id,
            status=request.status,
            note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{investigation_id}/verifications/{verification_id}")
def update_verification_status(
    investigation_id: str,
    verification_id: str,
    request: UpdateVerificationStatusRequest,
):
    container = get_container()
    try:
        return container.repository.update_verification_status(
            investigation_id,
            verification_id,
            status=request.status,
            result_note=request.result_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{investigation_id}", response_model=InvestigationRecord)
def get_investigation(investigation_id: str) -> InvestigationRecord:
    container = get_container()
    try:
        return container.repository.get(investigation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
