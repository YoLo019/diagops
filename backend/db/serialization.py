from collections.abc import Mapping, Sequence
from typing import Any

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.context import SpecialistResult
from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.llm_analysis import LLMAnalysis
from backend.domain.multi_agent import InvestigationStrategy, MultiAgentRunSummary
from backend.domain.reports import IncidentReport
from backend.providers.results import ProviderResult
from backend.safety.redaction import assert_safe_value, redact_value

_STRATEGY_KEY = "_diagops_investigation_strategy"
_MULTI_AGENT_RUN_KEY = "_diagops_multi_agent_run"
_ACTIVE_RUNTIME_RUN_KEY = "_diagops_active_runtime_run_id"
_SOURCE_INVESTIGATION_KEY = "_diagops_source_investigation_id"


def record_to_rows(record: InvestigationRecord) -> dict[str, Any]:
    event_payload = record.event.model_dump(mode="json")
    # V8.2 将 additive strategy 写入现有 JSON，避免为 V5 历史库增加物理迁移。
    investigation_event = {**event_payload, _STRATEGY_KEY: record.strategy.value}
    if record.multi_agent_run is not None:
        investigation_event[_MULTI_AGENT_RUN_KEY] = record.multi_agent_run.model_dump(
            mode="json"
        )
    if record.active_runtime_run_id is not None:
        investigation_event[_ACTIVE_RUNTIME_RUN_KEY] = record.active_runtime_run_id
    if record.source_investigation_id is not None:
        investigation_event[_SOURCE_INVESTIGATION_KEY] = record.source_investigation_id
    rows = {
        "investigation": {
            "id": record.id,
            "event": investigation_event,
            "status": record.status.value,
            "failure_reason": record.failure_reason,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "completed_at": (
                record.completed_at.isoformat() if record.completed_at is not None else None
            ),
        },
        "events": [event_payload],
        "evidence_items": _child_rows(record.id, record.evidence),
        "hypotheses": _child_rows(record.id, record.hypotheses),
        "recommended_actions": _child_rows(record.id, record.actions),
        "verification_suggestions": _child_rows(
            record.id, record.verification_suggestions
        ),
        "report": (
            redact_value(record.report.model_dump(mode="json"))
            if record.report
            else None
        ),
        "provider_results": _payload_rows(record.id, record.provider_results),
        "specialist_results": _payload_rows(record.id, record.specialist_results),
    }
    assert_safe_value(rows)
    return rows


def rows_to_record(rows: Mapping[str, Any]) -> InvestigationRecord:
    investigation = rows["investigation"]
    record = InvestigationRecord(
        id=investigation["id"],
        event=IncidentEvent(**investigation["event"]),
        strategy=investigation["event"].get(
            _STRATEGY_KEY, InvestigationStrategy.FIXED
        ),
        multi_agent_run=(
            MultiAgentRunSummary.model_validate(
                investigation["event"][_MULTI_AGENT_RUN_KEY]
            )
            if investigation["event"].get(_MULTI_AGENT_RUN_KEY) is not None
            else None
        ),
        active_runtime_run_id=investigation["event"].get(_ACTIVE_RUNTIME_RUN_KEY),
        source_investigation_id=investigation["event"].get(_SOURCE_INVESTIGATION_KEY),
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
        llm_analysis=(
            LLMAnalysis(**rows["llm_analysis"]["payload"])
            if rows.get("llm_analysis") is not None
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
    # 历史事件没有 payload-only owner 字段；移除显式 null 才能保持旧序列化字节。
    event_payload = investigation["event"]
    for field_name, payload_key in (
        ("active_runtime_run_id", _ACTIVE_RUNTIME_RUN_KEY),
        ("source_investigation_id", _SOURCE_INVESTIGATION_KEY),
    ):
        if payload_key not in event_payload:
            record.model_fields_set.discard(field_name)
    return record


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
