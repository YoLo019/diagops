from backend.diagnosis.action_planner import ActionPlanner
from backend.domain.actions import ActionRiskLevel, ActionType
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def plan_for_case(case_id: str):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    return ActionPlanner().plan(event, evidence, hypotheses)


def test_deployment_regression_generates_high_risk_rollback_suggestion():
    actions, verifications = plan_for_case("deployment_regression")

    top_action = actions[0]
    assert top_action.action_type == ActionType.ROLLBACK_SUGGESTION
    assert top_action.risk_level == ActionRiskLevel.HIGH
    assert top_action.requires_approval is True
    assert top_action.supporting_evidence_ids
    assert any("5xx" in item.expected_signal for item in verifications)


def test_traffic_spike_generates_scale_suggestion():
    actions, verifications = plan_for_case("traffic_spike")

    assert actions[0].action_type == ActionType.SCALE_SUGGESTION
    assert actions[0].risk_level == ActionRiskLevel.MEDIUM
    assert actions[0].requires_approval is True
    assert actions[0].supporting_evidence_ids
    assert any("QPS" in item.expected_signal for item in verifications)


def test_unknown_generates_manual_follow_up_action():
    event = load_incident_case("deployment_regression")
    actions, verifications = ActionPlanner().plan(
        event,
        evidence=[],
        hypotheses=[
            Hypothesis(
                cause_type=CauseType.UNKNOWN,
                summary="No evidence available.",
                confidence=0.2,
                supporting_evidence_ids=[],
                contradicting_evidence_ids=[],
                next_actions=[
                    "Collect logs, metrics, deploy records, and dependency status."
                ],
            ),
        ],
    )

    assert actions[0].action_type == ActionType.MANUAL_FOLLOW_UP
    assert actions[0].risk_level == ActionRiskLevel.LOW
    assert actions[0].requires_approval is False
    assert actions[0].supporting_evidence_ids
    assert verifications[0].expected_signal == "new evidence is collected"


def test_empty_evidence_generates_manual_follow_up_even_with_hypothesis_ids():
    event = load_incident_case("deployment_regression")
    actions, verifications = ActionPlanner().plan(
        event,
        evidence=[],
        hypotheses=[
            Hypothesis(
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="Deployment looks suspicious.",
                confidence=0.8,
                supporting_evidence_ids=["ev-stale"],
                contradicting_evidence_ids=[],
                next_actions=["Check deployment."],
            ),
        ],
    )

    assert actions[0].action_type == ActionType.MANUAL_FOLLOW_UP
    assert actions[0].risk_level == ActionRiskLevel.LOW
    assert actions[0].requires_approval is False
    assert verifications[0].expected_signal == "new evidence is collected"
