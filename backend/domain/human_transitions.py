from __future__ import annotations

from typing import TYPE_CHECKING

from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)
from backend.domain.evidence import EvidenceStatus
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import AuthorityMode
from backend.providers.results import ProviderStatus
from backend.safety.redaction import redact_text

if TYPE_CHECKING:
    from backend.db.models import InvestigationRecord


_SAFE_DETAILS = {
    "action_transition": "action state transition is not allowed",
    "approval_note_required": "high-risk approval requires a note",
    "verification_transition": "verification state transition is not allowed",
    "verification_result_note_required": "verification result requires a note",
    "verification_result_evidence_required": "verification result requires valid evidence",
    "verification_relation_required": "verification result requires an action or cause",
    "verification_reference": "verification reference or runtime owner is invalid",
    "skipped_verification_evidence": "skipped verification cannot claim result evidence",
    "action_reference": "action candidate reference or runtime owner is invalid",
}


class HumanStateConflict(ValueError):
    """表示请求格式正确，但人工状态或同调查引用不允许该更新。"""

    def __init__(self, code: str) -> None:
        self.code = code
        self.detail = _SAFE_DETAILS[code]
        super().__init__(f"{code}: {self.detail}")


def validate_action_transition(
    record: InvestigationRecord,
    action_id: str,
    target: ActionStatus | str,
    note: str | None,
) -> RecommendedAction:
    """验证 action 状态机，并保留 high-risk 的人工审批说明。"""
    action = next((item for item in record.actions if item.id == action_id), None)
    if action is None:
        raise ValueError(f"Unknown action: {action_id}")
    _validate_runtime_owner(record, action, "action_reference")
    _validate_candidate_refs(record, action.related_candidate_ids, "action_reference")
    target = ActionStatus(target)
    allowed = (
        {
            ActionStatus.PROPOSED: {
                ActionStatus.APPROVED,
                ActionStatus.REJECTED,
                ActionStatus.SKIPPED,
            },
            ActionStatus.APPROVED: {ActionStatus.DONE, ActionStatus.SKIPPED},
        }
        if action.requires_approval
        else {
            ActionStatus.PROPOSED: {
                ActionStatus.DONE,
                ActionStatus.REJECTED,
                ActionStatus.SKIPPED,
            }
        }
    )
    if target not in allowed.get(action.status, set()):
        raise HumanStateConflict("action_transition")

    sanitized_note = redact_text(note.strip()) if note and note.strip() else None
    if (
        action.risk_level == ActionRiskLevel.HIGH
        and target == ActionStatus.APPROVED
        and sanitized_note is None
    ):
        raise HumanStateConflict("approval_note_required")
    if (
        action.risk_level == ActionRiskLevel.HIGH
        and action.status == ActionStatus.APPROVED
        and target == ActionStatus.DONE
        and not (action.note and action.note.strip())
    ):
        raise HumanStateConflict("approval_note_required")
    if action.status == ActionStatus.APPROVED:
        sanitized_note = action.note
    return action.model_copy(update={"status": target, "note": sanitized_note})


def validate_verification_transition(
    record: InvestigationRecord,
    verification_id: str,
    target: VerificationStatus | str,
    result_note: str | None,
    result_evidence_ids: list[str],
    related_action_ids: list[str],
    related_cause_types: list[CauseType | str],
    related_candidate_ids: list[str] | None = None,
) -> VerificationSuggestion:
    """验证 verification 结果仅引用同一调查内的有效事实与人工对象。"""
    verification = next(
        (item for item in record.verification_suggestions if item.id == verification_id),
        None,
    )
    if verification is None:
        raise ValueError(f"Unknown verification suggestion: {verification_id}")
    _validate_runtime_owner(record, verification, "verification_reference")
    candidate_ids = list(
        verification.related_candidate_ids
        if related_candidate_ids is None
        else related_candidate_ids
    )
    if (
        verification.runtime_run_id is not None
        and related_candidate_ids is not None
        and candidate_ids != verification.related_candidate_ids
    ):
        # V11 verification 是对已持久化候选的结果回填，人工请求不能借此
        # 把结果重新绑定到同一投影中的另一个候选。
        raise HumanStateConflict("verification_reference")
    _validate_candidate_refs(record, verification.related_candidate_ids, "verification_reference")
    _validate_candidate_refs(record, candidate_ids, "verification_reference")
    target = VerificationStatus(target)
    if verification.status != VerificationStatus.PENDING or target not in {
        VerificationStatus.PASSED,
        VerificationStatus.FAILED,
        VerificationStatus.SKIPPED,
    }:
        raise HumanStateConflict("verification_transition")

    note = redact_text(result_note.strip()) if result_note and result_note.strip() else None
    cause_types = [CauseType(item) for item in related_cause_types]
    if verification.runtime_run_id is not None and cause_types:
        raise HumanStateConflict("verification_reference")
    eligible_evidence = {
        item.id: item
        for item in record.evidence
        if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
    }
    valid_evidence = {
        item.id
        for result in record.provider_results
        if result.status in {ProviderStatus.SUCCESS, ProviderStatus.PARTIAL}
        for item in result.evidence_items
        if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        and item.provider == result.provider
        and item.id in eligible_evidence
        and eligible_evidence[item.id].provider == result.provider
    }
    valid_actions = {item.id for item in record.actions}
    valid_causes = {item.cause_type for item in record.hypotheses}
    if (
        not set(result_evidence_ids) <= valid_evidence
        or not set(related_action_ids) <= valid_actions
        or not set(cause_types) <= valid_causes
    ):
        raise HumanStateConflict("verification_reference")
    if verification.runtime_run_id is not None:
        related_actions = {item.id: item for item in record.actions}
        if any(
            related_actions[action_id].runtime_run_id != verification.runtime_run_id
            or (
                related_actions[action_id].related_candidate_ids
                and not set(related_actions[action_id].related_candidate_ids)
                <= set(candidate_ids)
            )
            for action_id in related_action_ids
        ):
            raise HumanStateConflict("verification_reference")

    if target in {VerificationStatus.PASSED, VerificationStatus.FAILED}:
        if note is None:
            raise HumanStateConflict("verification_result_note_required")
        if not result_evidence_ids:
            raise HumanStateConflict("verification_result_evidence_required")
        if not related_action_ids and not cause_types and not candidate_ids:
            raise HumanStateConflict("verification_relation_required")
    elif result_evidence_ids:
        raise HumanStateConflict("skipped_verification_evidence")

    return verification.model_copy(
        update={
            "status": target,
            "result_note": note,
            "result_evidence_ids": list(dict.fromkeys(result_evidence_ids)),
            "related_action_ids": list(dict.fromkeys(related_action_ids)),
            "related_cause_types": list(dict.fromkeys(cause_types)),
            "related_candidate_ids": list(dict.fromkeys(candidate_ids)),
        }
    )


def _validate_runtime_owner(
    record: InvestigationRecord,
    projection: object,
    error_code: str,
) -> None:
    active_owner = getattr(record, "active_runtime_run_id", None)
    projection_owner = getattr(projection, "runtime_run_id", None)
    if active_owner is not None or projection_owner is not None:
        if active_owner != projection_owner:
            raise HumanStateConflict(error_code)


def _validate_candidate_refs(
    record: InvestigationRecord,
    candidate_ids: list[str],
    error_code: str,
) -> None:
    if not candidate_ids:
        return
    report = record.report
    if (
        report is None
        or report.authority_mode != AuthorityMode.AGENT
        or report.runtime_run_id != getattr(record, "active_runtime_run_id", None)
    ):
        raise HumanStateConflict(error_code)
    known_ids = {candidate.id for candidate in report.diagnoses + report.alternatives}
    if not set(candidate_ids) <= known_ids:
        raise HumanStateConflict(error_code)
