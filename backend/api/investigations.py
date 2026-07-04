from fastapi import APIRouter, HTTPException

from backend.db.models import InvestigationRecord
from backend.services.container import get_container

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.get("", response_model=list[InvestigationRecord])
def list_investigations() -> list[InvestigationRecord]:
    container = get_container()
    return container.repository.list()


@router.get("/{investigation_id}", response_model=InvestigationRecord)
def get_investigation(investigation_id: str) -> InvestigationRecord:
    container = get_container()
    try:
        return container.repository.get(investigation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
