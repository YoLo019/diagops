from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.db.models import InvestigationRecord, InvestigationSummary
from backend.domain.events import IncidentEvent
from backend.domain.multi_agent import InvestigationStrategy
from backend.safety.redaction import assert_safe_label
from backend.services.container import get_container
from backend.services.incident_cases import list_case_ids, load_incident_case
from backend.services.v11_projection import (
    V11ProjectionIntegrityError,
    ensure_v11_projection_owner,
)
from backend.services.v11_public import public_v11_review

router = APIRouter(prefix="/events", tags=["events"])


class AlertmanagerEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    alerts: list[object] = Field(max_length=100)


class AlertmanagerAlertResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["created", "ignored", "failed"]
    alert_index: int = Field(ge=0)
    investigation: InvestigationSummary | None = None
    failure_category: Literal["invalid_alert", "investigation_failed"] | None = None
    detail: str | None = None


def to_summary(
    record: InvestigationRecord,
    *,
    runtime_available: bool | None = None,
    coordination_review=None,
) -> InvestigationSummary:
    review = coordination_review
    if review is not None and review.authority_mode.value == "agent":
        review = public_v11_review(review)
    summary = InvestigationSummary.from_record(record, review)
    if runtime_available is None:
        return summary
    return summary.model_copy(update={"runtime_available": runtime_available})


@router.post("", response_model=InvestigationSummary)
async def create_event(
    event: IncidentEvent,
    strategy: InvestigationStrategy | None = None,
) -> InvestigationSummary:
    container = get_container()
    record = await container.run_investigation(
        event,
        strategy=strategy,
        execution_contract_version=container.product_execution_contract_version(),
    )
    try:
        ensure_v11_projection_owner(container.repository, container.runtime_store, record)
    except V11ProjectionIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return to_summary(
        record,
        runtime_available=bool(container.runtime_store.list_runs(record.id)),
        coordination_review=container.repository.get_coordination_review(record.id),
    )


@router.post(
    "/alertmanager",
    response_model=list[AlertmanagerAlertResult],
)
async def create_alertmanager_events(
    body: object = Body(...),
) -> list[AlertmanagerAlertResult]:
    try:
        envelope = AlertmanagerEnvelope.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="Alertmanager envelope is invalid",
        ) from exc

    container = get_container()
    results = []
    for index, alert in enumerate(envelope.alerts):
        if isinstance(alert, dict) and alert.get("status") == "resolved":
            results.append(
                AlertmanagerAlertResult(
                    status="ignored",
                    alert_index=index,
                )
            )
            continue
        try:
            event = _alertmanager_event(alert)
        except (TypeError, ValueError):
            results.append(
                AlertmanagerAlertResult(
                    status="failed",
                    alert_index=index,
                    failure_category="invalid_alert",
                    detail="Alert payload is invalid",
                )
            )
            continue
        try:
            record = await container.run_investigation(
                event,
                execution_contract_version=container.product_execution_contract_version(),
            )
            ensure_v11_projection_owner(
                container.repository, container.runtime_store, record
            )
        except Exception:
            results.append(
                AlertmanagerAlertResult(
                    status="failed",
                    alert_index=index,
                    failure_category="investigation_failed",
                    detail="Investigation failed",
                )
            )
            continue
        results.append(
            AlertmanagerAlertResult(
                status="created",
                alert_index=index,
                investigation=to_summary(
                    record,
                    runtime_available=bool(container.runtime_store.list_runs(record.id)),
                    coordination_review=container.repository.get_coordination_review(
                        record.id
                    ),
                ),
            )
        )
    return results


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
    record = await container.run_investigation(
        event,
        execution_contract_version=container.product_execution_contract_version(),
    )
    try:
        ensure_v11_projection_owner(container.repository, container.runtime_store, record)
    except V11ProjectionIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return to_summary(
        record,
        runtime_available=bool(container.runtime_store.list_runs(record.id)),
        coordination_review=container.repository.get_coordination_review(record.id),
    )


def _alertmanager_event(raw_alert: object) -> IncidentEvent:
    if not isinstance(raw_alert, dict) or raw_alert.get("status") != "firing":
        raise ValueError("invalid alert")
    labels = raw_alert.get("labels")
    annotations = raw_alert.get("annotations", {})
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise ValueError("invalid alert")

    service = _bounded_string(labels.get("service"), 256)
    environment = _bounded_string(labels.get("environment"), 256)
    assert_safe_label(service)
    assert_safe_label(environment)
    summary = annotations.get("summary")
    title = _bounded_string(
        summary if summary not in (None, "") else labels.get("alertname"),
        512,
    )
    description = annotations.get("description", "")
    if not isinstance(description, str) or len(description) > 4096:
        raise ValueError("invalid alert")
    started_at = raw_alert.get("startsAt")
    if not isinstance(started_at, str):
        raise ValueError("invalid alert")
    severity = labels.get("severity")
    event = IncidentEvent.model_validate(
        {
            "source": "webhook",
            "service": service,
            "environment": environment,
            "severity": (severity if severity in {"critical", "warning"} else "info"),
            "title": title,
            "description": description,
            "started_at": started_at,
            "time_window_minutes": 30,
            "signals": {},
        }
    )
    if event.started_at.utcoffset() is None:
        raise ValueError("invalid alert")
    return event


def _bounded_string(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid alert")
    return value
