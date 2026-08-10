from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.api.events import to_summary
from backend.db.models import InvestigationRecord, InvestigationSummary
from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceProvider
from backend.domain.human_transitions import HumanStateConflict
from backend.domain.hypotheses import CauseType
from backend.domain.memory import MemoryItem
from backend.domain.multi_agent import AuthorityMode, InvestigationStrategy
from backend.domain.reports import IncidentReport
from backend.memory import MemoryStore
from backend.providers.results import ProviderResult
from backend.safety.redaction import assert_safe_label, redact_model, redact_text
from backend.services.container import get_container
from backend.services.v11_projection import (
    V11ProjectionIntegrityError,
    ensure_v11_projection_owner,
)
from backend.services.v11_public import (
    public_v11_action,
    public_v11_report,
    public_v11_verification,
)

router = APIRouter(prefix="/investigations", tags=["investigations"])


class ManualInvestigationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1)
    service: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    strategy: InvestigationStrategy | None = None

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        """在公开输入边界拒绝凭据，避免进入业务记录和 runtime snapshot。"""
        assert_safe_label(value)
        return value


class UpdateActionStatusRequest(BaseModel):
    status: ActionStatus
    note: str | None = None


class UpdateVerificationStatusRequest(BaseModel):
    status: VerificationStatus
    result_note: str | None = None
    result_evidence_ids: list[str] = Field(default_factory=list)
    related_action_ids: list[str] = Field(default_factory=list)
    related_cause_types: list[CauseType] = Field(default_factory=list)
    related_candidate_ids: list[str] | None = None


class FeedbackRequest(BaseModel):
    root_cause_correct: bool | None = None
    action_useful: bool | None = None
    verification_result: str | None = None
    note: str | None = None


def _get_investigation_record(investigation_id: str) -> InvestigationRecord:
    container = get_container()
    try:
        stored = container.repository.get(investigation_id)
        try:
            ensure_v11_projection_owner(
                container.repository, container.runtime_store, stored
            )
        except V11ProjectionIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record = _public_investigation_record(stored)
        return record.model_copy(
            update={"runtime_available": _runtime_available(investigation_id)}
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _sorted_evidence(record: InvestigationRecord) -> list[EvidenceItem]:
    return sorted(record.evidence, key=lambda item: (item.timestamp, item.id))


def _public_investigation_record(record: InvestigationRecord) -> InvestigationRecord:
    projected = redact_model(record)
    if record.report is None or record.report.authority_mode != AuthorityMode.AGENT:
        return projected
    return projected.model_copy(
        update={
            "report": public_v11_report(projected.report),
            "actions": [public_v11_action(item) for item in projected.actions],
            "verification_suggestions": [
                public_v11_verification(item)
                for item in projected.verification_suggestions
            ],
        }
    )


def _runtime_available(investigation_id: str) -> bool:
    container = get_container()
    return bool(container.runtime_store.list_runs(investigation_id))


@router.get("", response_model=list[InvestigationRecord])
def list_investigations() -> list[InvestigationRecord]:
    container = get_container()
    records = []
    for record in container.repository.list():
        try:
            ensure_v11_projection_owner(
                container.repository, container.runtime_store, record
            )
        except V11ProjectionIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        records.append(
            _public_investigation_record(record).model_copy(
            update={"runtime_available": _runtime_available(record.id)}
            )
        )
    return records


@router.get("/summaries", response_model=list[InvestigationSummary])
def list_investigation_summaries() -> list[InvestigationSummary]:
    container = get_container()
    summaries = []
    for item in container.repository.list_summaries():
        record = container.repository.get(item.id)
        try:
            ensure_v11_projection_owner(
                container.repository, container.runtime_store, record
            )
        except V11ProjectionIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        summaries.append(
            to_summary(
                record,
                runtime_available=_runtime_available(record.id),
                coordination_review=container.repository.get_coordination_review(
                    record.id
                ),
            )
        )
    return summaries


@router.post("/manual", response_model=InvestigationSummary)
async def create_manual_investigation(
    request: ManualInvestigationRequest,
) -> InvestigationSummary:
    text = request.text.strip()
    service = request.service.strip()
    environment = request.environment.strip()
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service=service,
        environment=environment,
        severity=Severity.WARNING,
        title=text[:80],
        description=text,
        started_at=datetime.now(UTC),
    )
    container = get_container()
    record = await container.run_investigation(
        event,
        strategy=request.strategy,
        execution_contract_version=container.product_execution_contract_version(),
    )
    try:
        ensure_v11_projection_owner(container.repository, container.runtime_store, record)
    except V11ProjectionIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return to_summary(
        record,
        runtime_available=_runtime_available(record.id),
        coordination_review=container.repository.get_coordination_review(record.id),
    )


@router.patch("/{investigation_id}/actions/{action_id}")
def update_action_status(
    investigation_id: str,
    action_id: str,
    request: UpdateActionStatusRequest,
):
    container = get_container()
    try:
        updated = container.repository.update_action_status(
            investigation_id,
            action_id,
            status=request.status,
            note=redact_text(request.note) if request.note is not None else None,
        )
        return redact_model(updated)
    except HumanStateConflict as exc:
        return JSONResponse(
            status_code=409,
            content={"code": exc.code, "detail": exc.detail},
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
        updated = container.repository.update_verification_status(
            investigation_id,
            verification_id,
            status=request.status,
            result_note=(
                redact_text(request.result_note)
                if request.result_note is not None
                else None
            ),
            result_evidence_ids=request.result_evidence_ids,
            related_action_ids=request.related_action_ids,
            related_cause_types=request.related_cause_types,
            related_candidate_ids=request.related_candidate_ids,
        )
        return redact_model(updated)
    except HumanStateConflict as exc:
        return JSONResponse(
            status_code=409,
            content={"code": exc.code, "detail": exc.detail},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{investigation_id}/feedback", response_model=MemoryItem)
def record_feedback(
    investigation_id: str,
    request: FeedbackRequest,
) -> MemoryItem:
    record = _get_investigation_record(investigation_id)
    container = get_container()
    return MemoryStore(container.repository).record_feedback(
        record,
        request.root_cause_correct,
        request.action_useful,
        request.verification_result,
        request.note,
    )


@router.get("/{investigation_id}/timeline", response_model=list[EvidenceItem])
def get_investigation_timeline(investigation_id: str) -> list[EvidenceItem]:
    record = _get_investigation_record(investigation_id)
    return _sorted_evidence(record)


@router.get("/{investigation_id}/evidence", response_model=list[EvidenceItem])
def get_investigation_evidence(
    investigation_id: str,
    provider: EvidenceProvider | None = None,
) -> list[EvidenceItem]:
    record = _get_investigation_record(investigation_id)
    evidence = record.evidence
    if provider is not None:
        return [item for item in evidence if item.provider == provider]
    return evidence


@router.get("/{investigation_id}/provider-results", response_model=list[ProviderResult])
def get_investigation_provider_results(investigation_id: str) -> list[ProviderResult]:
    record = _get_investigation_record(investigation_id)
    return record.provider_results


@router.get("/{investigation_id}/specialist-results", response_model=list[SpecialistResult])
def get_investigation_specialist_results(investigation_id: str) -> list[SpecialistResult]:
    record = _get_investigation_record(investigation_id)
    return record.specialist_results


@router.get("/{investigation_id}/report", response_model=IncidentReport | None)
def get_investigation_report(investigation_id: str) -> IncidentReport | None:
    record = _get_investigation_record(investigation_id)
    return record.report


@router.get("/{investigation_id}", response_model=InvestigationRecord)
def get_investigation(investigation_id: str) -> InvestigationRecord:
    return _get_investigation_record(investigation_id)
