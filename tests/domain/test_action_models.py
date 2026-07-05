import pytest
from pydantic import ValidationError

from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)


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
