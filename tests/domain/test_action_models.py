import pytest
from pydantic import ValidationError

from backend.db.models import InvestigationRecord
from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.human_transitions import (
    HumanStateConflict,
    validate_action_transition,
    validate_verification_transition,
)
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.results import ProviderResult, ProviderStatus
from backend.services.incident_cases import load_incident_case


def test_recommended_action_defaults_to_proposed_and_requires_evidence():
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Deployment evidence and new errors point to a regression.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-deploy", "ev-log"],
    )

    assert action.id.startswith("act-")
    assert action.status == ActionStatus.PROPOSED
    assert action.supporting_evidence_ids == ["ev-deploy", "ev-log"]


def test_recommended_action_rejects_high_risk_without_approval():
    with pytest.raises(ValidationError, match="medium and high risk actions require approval"):
        RecommendedAction(
            action_type=ActionType.RESTART_SUGGESTION,
            title="Restart service",
            description="Restart may affect traffic.",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=False,
            supporting_evidence_ids=["ev-log"],
        )


def test_recommended_action_rejects_empty_evidence_ids():
    with pytest.raises(ValidationError, match="supporting_evidence_ids must not be empty"):
        RecommendedAction(
            action_type=ActionType.MANUAL_FOLLOW_UP,
            title="Ask owner",
            description="Need a human to confirm business impact.",
            risk_level=ActionRiskLevel.LOW,
            requires_approval=False,
            supporting_evidence_ids=[],
        )


def test_verification_suggestion_defaults_to_pending():
    suggestion = VerificationSuggestion(
        title="Check 5xx recovery",
        description="Confirm error rate returned to normal.",
        expected_signal="5xx rate below 1%",
    )

    assert suggestion.id.startswith("ver-")
    assert suggestion.status == VerificationStatus.PENDING
    assert suggestion.result_note is None
    assert suggestion.result_evidence_ids == []
    assert suggestion.related_action_ids == []
    assert suggestion.related_cause_types == []


@pytest.mark.parametrize(
    ("requires_approval", "current", "target"),
    [
        (False, ActionStatus.PROPOSED, ActionStatus.DONE),
        (False, ActionStatus.PROPOSED, ActionStatus.REJECTED),
        (False, ActionStatus.PROPOSED, ActionStatus.SKIPPED),
        (True, ActionStatus.PROPOSED, ActionStatus.APPROVED),
        (True, ActionStatus.PROPOSED, ActionStatus.REJECTED),
        (True, ActionStatus.PROPOSED, ActionStatus.SKIPPED),
        (True, ActionStatus.APPROVED, ActionStatus.DONE),
        (True, ActionStatus.APPROVED, ActionStatus.SKIPPED),
    ],
)
def test_allowed_action_transition_matrix(
    requires_approval: bool, current: ActionStatus, target: ActionStatus
) -> None:
    record = _record(requires_approval=requires_approval, action_status=current)

    updated = validate_action_transition(
        record,
        record.actions[0].id,
        target,
        "owner approved" if requires_approval else None,
    )

    assert updated.status == target


@pytest.mark.parametrize(
    ("requires_approval", "current", "target"),
    [
        (False, ActionStatus.PROPOSED, ActionStatus.APPROVED),
        (True, ActionStatus.PROPOSED, ActionStatus.DONE),
        (True, ActionStatus.APPROVED, ActionStatus.REJECTED),
        (True, ActionStatus.DONE, ActionStatus.SKIPPED),
        (False, ActionStatus.REJECTED, ActionStatus.DONE),
        (False, ActionStatus.SKIPPED, ActionStatus.REJECTED),
    ],
)
def test_disallowed_action_transition_matrix(
    requires_approval: bool, current: ActionStatus, target: ActionStatus
) -> None:
    record = _record(requires_approval=requires_approval, action_status=current)

    with pytest.raises(HumanStateConflict, match="action_transition"):
        validate_action_transition(record, record.actions[0].id, target, "note")


def test_high_risk_approval_requires_note_and_done_retains_it() -> None:
    record = _record(requires_approval=True)

    with pytest.raises(HumanStateConflict, match="approval_note_required"):
        validate_action_transition(
            record, record.actions[0].id, ActionStatus.APPROVED, "   "
        )

    approved = validate_action_transition(
        record, record.actions[0].id, ActionStatus.APPROVED, "owner approved"
    )
    record.actions[0] = approved
    done = validate_action_transition(
        record, approved.id, ActionStatus.DONE, None
    )

    assert done.note == "owner approved"


@pytest.mark.parametrize(
    "target", [VerificationStatus.PASSED, VerificationStatus.FAILED]
)
def test_verification_result_requires_note_and_valid_references(
    target: VerificationStatus,
) -> None:
    record = _record()
    verification = record.verification_suggestions[0]

    updated = validate_verification_transition(
        record,
        verification.id,
        target,
        "signal recovered",
        [record.evidence[0].id],
        [record.actions[0].id],
        [],
    )

    assert updated.status == target
    assert updated.result_evidence_ids == [record.evidence[0].id]
    assert updated.related_action_ids == [record.actions[0].id]


def test_verification_rejects_missing_result_contract_and_terminal_rewrite() -> None:
    record = _record()
    verification = record.verification_suggestions[0]

    with pytest.raises(HumanStateConflict, match="verification_result_note_required"):
        validate_verification_transition(
            record, verification.id, VerificationStatus.PASSED, " ", [], [], []
        )

    record.verification_suggestions[0] = verification.model_copy(
        update={"status": VerificationStatus.PASSED}
    )
    with pytest.raises(HumanStateConflict, match="verification_transition"):
        validate_verification_transition(
            record,
            verification.id,
            VerificationStatus.FAILED,
            "changed",
            [record.evidence[0].id],
            [],
            [CauseType.DEPLOYMENT_REGRESSION],
        )


def test_verification_rejects_evidence_owned_by_failed_provider_result() -> None:
    record = _record(provider_status=ProviderStatus.FAILED)
    verification = record.verification_suggestions[0]

    with pytest.raises(HumanStateConflict, match="verification_reference"):
        validate_verification_transition(
            record,
            verification.id,
            VerificationStatus.PASSED,
            "signal recovered",
            [record.evidence[0].id],
            [],
            [CauseType.DEPLOYMENT_REGRESSION],
        )


def _record(
    *,
    requires_approval: bool = False,
    action_status: ActionStatus = ActionStatus.PROPOSED,
    provider_status: ProviderStatus = ProviderStatus.SUCCESS,
) -> InvestigationRecord:
    event = load_incident_case("deployment_regression")
    evidence = EvidenceItem(
        id="ev-result",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=event.started_at,
        summary="deployment evidence",
    )
    action = RecommendedAction(
        id="act-result",
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Record operator decision",
        description="Human-owned state only.",
        risk_level=(ActionRiskLevel.HIGH if requires_approval else ActionRiskLevel.LOW),
        requires_approval=requires_approval,
        supporting_evidence_ids=[evidence.id],
        status=action_status,
        note="owner approved" if action_status == ActionStatus.APPROVED else None,
    )
    verification = VerificationSuggestion(
        id="ver-result",
        title="Verify signal",
        description="Record observed outcome.",
        expected_signal="error rate recovers",
    )
    return InvestigationRecord(
        id="inv-result",
        event=event,
        evidence=[evidence],
        provider_results=[
            ProviderResult(
                provider=evidence.provider,
                status=provider_status,
                evidence_items=[evidence],
            )
        ],
        hypotheses=[
            Hypothesis(
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="deployment regression",
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
            )
        ],
        actions=[action],
        verification_suggestions=[verification],
    )
