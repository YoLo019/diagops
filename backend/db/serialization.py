from collections.abc import Mapping, Sequence
from typing import Any

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.reports import IncidentReport
from backend.providers.results import ProviderResult


def record_to_rows(record: InvestigationRecord) -> dict[str, Any]:
    return {
        "investigation": {
            "id": record.id,
            "event": record.event.model_dump(mode="json"),
            "status": record.status.value,
            "failure_reason": record.failure_reason,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at is not None else None
            ),
        },
        "events": [record.event.model_dump(mode="json")],
        "evidence_items": _child_rows(record.id, record.evidence),
        "hypotheses": _child_rows(record.id, record.hypotheses),
        "recommended_actions": _child_rows(record.id, record.actions),
        "verification_suggestions": _child_rows(
            record.id, record.verification_suggestions
        ),
        "report": record.report.model_dump(mode="json") if record.report else None,
        "provider_results": _payload_rows(record.id, record.provider_results),
        "specialist_results": _payload_rows(record.id, record.specialist_results),
        "llm_analysis": None,
    }


def rows_to_record(rows: Mapping[str, Any]) -> InvestigationRecord:
    investigation = rows["investigation"]
    return InvestigationRecord(
        id=investigation["id"],
        event=IncidentEvent(**investigation["event"]),
        status=InvestigationStatus(investigation["status"]),
        evidence=[
            EvidenceItem(**item["payload"])
            for item in _sorted_child_rows(rows.get("evidence_items", []))
        ],
        provider_results=[
            ProviderResult(**item["payload"])
            for item in _sorted_child_rows(rows.get("provider_results", []))
        ],
        specialist_results=[
            SpecialistResult(**item["payload"])
            for item in _sorted_child_rows(rows.get("specialist_results", []))
        ],
        hypotheses=[
            Hypothesis(**item["payload"])
            for item in _sorted_child_rows(rows.get("hypotheses", []))
        ],
        report=(
            IncidentReport(**rows["report"]["payload"])
            if rows.get("report") is not None
            else None
        ),
        failure_reason=investigation["failure_reason"],
        actions=[
            RecommendedAction(**item["payload"])
            for item in _sorted_child_rows(rows.get("recommended_actions", []))
        ],
        verification_suggestions=[
            VerificationSuggestion(**item["payload"])
            for item in _sorted_child_rows(rows.get("verification_suggestions", []))
        ],
        created_at=investigation["created_at"],
        updated_at=investigation["updated_at"],
        completed_at=investigation["completed_at"],
    )


def _child_rows(record_id: str, models: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": model.id,
            "investigation_id": record_id,
            "position": index,
            "payload": model.model_dump(mode="json"),
        }
        for index, model in enumerate(models)
    ]


def _payload_rows(record_id: str, models: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "investigation_id": record_id,
            "position": index,
            "payload": model.model_dump(mode="json"),
        }
        for index, model in enumerate(models)
    ]


def _sorted_child_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=lambda row: row["position"])
