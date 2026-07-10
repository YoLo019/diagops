from datetime import UTC, datetime

import pytest

from backend.diagnosis.coordination_review import (
    LOW_CONFIDENCE,
    build_coordination_review,
    build_hybrid_coordination_review,
    conflicting_agent_names,
    decide_hybrid_status,
)
from backend.domain.agent_findings import AgentFinding, AgentFindingType, AgentName
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    MultiAgentRunStatus,
)


def _hypothesis(cause_type: CauseType, confidence: float) -> Hypothesis:
    return Hypothesis(cause_type=cause_type, summary="Baseline", confidence=confidence)


def _sdk_finding(
    finding_id: str,
    agent_name: AgentName,
    cause_type: CauseType,
    *,
    confidence: float = 0.8,
    finding_type: AgentFindingType = AgentFindingType.ROOT_CAUSE,
    analysis_round: int = 1,
    revises_finding_id: str | None = None,
    investigation_id: str = "inv-1",
) -> AgentFinding:
    return AgentFinding(
        id=finding_id,
        investigation_id=investigation_id,
        agent_name=agent_name,
        finding_type=finding_type,
        summary=f"{agent_name} finding",
        confidence=confidence,
        evidence_ids=[f"ev-{finding_id}"],
        related_cause_type=cause_type,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        analysis_round=analysis_round,
        revises_finding_id=revises_finding_id,
    )


def test_multiple_findings_for_same_cause_rank_first_with_supporting_ids():
    findings = [
        _finding(
            "finding-log",
            AgentName.LOG,
            AgentFindingType.ROOT_CAUSE,
            CauseType.DEPLOYMENT_REGRESSION,
            0.7,
            ["ev-log"],
            "Log error started after deploy",
        ),
        _finding(
            "finding-deploy",
            AgentName.DEPLOYMENT,
            AgentFindingType.ROOT_CAUSE,
            CauseType.DEPLOYMENT_REGRESSION,
            0.8,
            ["ev-deploy"],
            "Deploy changed checkout",
        ),
        _finding(
            "finding-metric",
            AgentName.METRIC,
            AgentFindingType.ROOT_CAUSE,
            CauseType.TRAFFIC_SPIKE,
            0.6,
            ["ev-metric"],
            "Traffic rose",
        ),
    ]

    review = build_coordination_review(
        "inv-1",
        findings,
        [_evidence("ev-log"), _evidence("ev-deploy"), _evidence("ev-metric")],
        [],
    )

    top = review.candidates[0]
    assert top.rank == 1
    assert top.cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert top.supporting_finding_ids == ["finding-log", "finding-deploy"]
    second = review.candidates[1]
    assert second.rank == 2
    assert "candidate #2" in second.summary


def test_unknown_finding_evidence_id_raises_value_error():
    finding = _finding(
        "finding-log",
        AgentName.LOG,
        AgentFindingType.ROOT_CAUSE,
        CauseType.DATABASE_SLOWDOWN,
        0.7,
        ["ev-missing"],
        "Slow queries",
    )

    with pytest.raises(ValueError, match="Unknown evidence id"):
        build_coordination_review("inv-1", [finding], [_evidence("ev-known")], [])


def test_no_findings_seeds_candidates_from_hypotheses_with_limited_uncertainty():
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            summary="Payments timed out",
            confidence=0.6,
            supporting_evidence_ids=["ev-support"],
            contradicting_evidence_ids=["ev-against"],
            next_actions=["Check dependency status"],
        )
    ]

    review = build_coordination_review(
        "inv-1",
        [],
        [_evidence("ev-support"), _evidence("ev-against")],
        hypotheses,
    )

    assert len(review.candidates) == 1
    candidate = review.candidates[0]
    assert candidate.cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE
    assert candidate.rank == 1
    assert candidate.confidence == 0.6
    assert candidate.supporting_evidence_ids == ["ev-support"]
    assert candidate.contradicting_evidence_ids == ["ev-against"]
    assert "limited specialist findings" in candidate.uncertainty


def test_unknown_hypothesis_evidence_id_raises_value_error_without_findings():
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.UNKNOWN,
            summary="Unknown seed",
            confidence=0.4,
            supporting_evidence_ids=["ev-missing"],
        )
    ]

    with pytest.raises(ValueError, match="Unknown evidence id"):
        build_coordination_review("inv-1", [], [_evidence("ev-known")], hypotheses)


@pytest.mark.parametrize(
    ("baseline", "findings", "run_status", "expected"),
    [
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [
                _sdk_finding("log", AgentName.LOG, CauseType.DEPLOYMENT_REGRESSION),
                _sdk_finding(
                    "metric", AgentName.METRIC, CauseType.DEPLOYMENT_REGRESSION
                ),
            ],
            MultiAgentRunStatus.COMPLETED,
            CoordinationDecisionStatus.AGREEMENT,
        ),
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [_sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE)],
            MultiAgentRunStatus.COMPLETED,
            CoordinationDecisionStatus.CONFLICT,
        ),
        (
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [
                _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
                _sdk_finding("metric", AgentName.METRIC, CauseType.DATABASE_SLOWDOWN),
            ],
            MultiAgentRunStatus.COMPLETED,
            CoordinationDecisionStatus.CONFLICT,
        ),
        (
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [
                _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
                _sdk_finding("metric", AgentName.METRIC, CauseType.TRAFFIC_SPIKE),
            ],
            MultiAgentRunStatus.COMPLETED,
            CoordinationDecisionStatus.AGENT_LEADS,
        ),
        (
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [_sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE)],
            MultiAgentRunStatus.COMPLETED,
            CoordinationDecisionStatus.FALLBACK,
        ),
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [
                _sdk_finding("log", AgentName.LOG, CauseType.DEPLOYMENT_REGRESSION),
                _sdk_finding(
                    "metric", AgentName.METRIC, CauseType.DEPLOYMENT_REGRESSION
                ),
            ],
            MultiAgentRunStatus.PARTIAL,
            CoordinationDecisionStatus.FALLBACK,
        ),
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [_sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE)],
            MultiAgentRunStatus.FAILED,
            CoordinationDecisionStatus.FALLBACK,
        ),
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [_sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE)],
            MultiAgentRunStatus.SKIPPED,
            CoordinationDecisionStatus.FALLBACK,
        ),
    ],
    ids=[
        "agreement",
        "baseline-conflict",
        "specialist-conflict",
        "agent-leads",
        "insufficient-support",
        "partial-cannot-agree",
        "failed",
        "skipped",
    ],
)
def test_decide_hybrid_status_matrix(baseline, findings, run_status, expected):
    assert decide_hybrid_status(baseline, findings, run_status) == expected


def test_low_confidence_boundary_is_strict_and_finding_at_boundary_is_active():
    baseline = _hypothesis(CauseType.TRAFFIC_SPIKE, LOW_CONFIDENCE)
    findings = [
        _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE, confidence=0.5),
        _sdk_finding("metric", AgentName.METRIC, CauseType.TRAFFIC_SPIKE),
    ]

    assert (
        decide_hybrid_status(baseline, findings, MultiAgentRunStatus.COMPLETED)
        == CoordinationDecisionStatus.AGREEMENT
    )


def test_support_counts_distinct_specialists_not_findings():
    findings = [
        _sdk_finding("log-1", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding("log-2", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
    ]

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            findings,
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.FALLBACK
    )


def test_active_contradiction_against_agent_leading_cause_is_conflict():
    findings = [
        _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding("metric", AgentName.METRIC, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "deploy",
            AgentName.DEPLOYMENT,
            CauseType.TRAFFIC_SPIKE,
            finding_type=AgentFindingType.CONTRADICTION,
        ),
    ]

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            findings,
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.CONFLICT
    )


def test_round_two_replaces_round_one_for_decision_and_candidates():
    findings = [
        _sdk_finding("log-r1", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "log-r2",
            AgentName.LOG,
            CauseType.DEPLOYMENT_REGRESSION,
            analysis_round=2,
            revises_finding_id="log-r1",
        ),
        _sdk_finding("metric", AgentName.METRIC, CauseType.DEPLOYMENT_REGRESSION),
    ]

    review = build_hybrid_coordination_review(
        "inv-1",
        findings,
        [_evidence("ev-log-r1"), _evidence("ev-log-r2"), _evidence("ev-metric")],
        [_hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8)],
        MultiAgentRunStatus.COMPLETED,
        "Agent review",
        "No material uncertainty",
    )

    assert review.decision_status == CoordinationDecisionStatus.AGREEMENT
    assert review.baseline_cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert review.selected_cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert review.candidates[0].supporting_finding_ids == ["log-r2", "metric"]
    assert all(
        "log-r1" not in candidate.supporting_finding_ids
        for candidate in review.candidates
    )


@pytest.mark.parametrize(
    "replacement",
    [
        _sdk_finding(
            "wrong-agent",
            AgentName.METRIC,
            CauseType.TRAFFIC_SPIKE,
            analysis_round=2,
            revises_finding_id="original",
        ),
        _sdk_finding(
            "wrong-investigation",
            AgentName.LOG,
            CauseType.TRAFFIC_SPIKE,
            analysis_round=2,
            revises_finding_id="original",
            investigation_id="inv-2",
        ),
    ],
    ids=["cross-agent", "cross-investigation"],
)
def test_cross_record_revision_raises_value_error(replacement):
    original = _sdk_finding("original", AgentName.LOG, CauseType.TRAFFIC_SPIKE)

    with pytest.raises(ValueError, match="revision"):
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [original, replacement],
            MultiAgentRunStatus.COMPLETED,
        )


def test_unknown_revision_id_raises_value_error():
    finding = _sdk_finding(
        "replacement",
        AgentName.LOG,
        CauseType.TRAFFIC_SPIKE,
        analysis_round=2,
        revises_finding_id="missing",
    )

    with pytest.raises(ValueError, match="Unknown revision id"):
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [finding],
            MultiAgentRunStatus.COMPLETED,
        )


def test_revision_of_round_two_finding_raises_value_error():
    findings = [
        _sdk_finding("round-1", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "round-2",
            AgentName.LOG,
            CauseType.TRAFFIC_SPIKE,
            analysis_round=2,
            revises_finding_id="round-1",
        ),
        _sdk_finding(
            "invalid",
            AgentName.LOG,
            CauseType.DATABASE_SLOWDOWN,
            analysis_round=2,
            revises_finding_id="round-2",
        ),
    ]

    with pytest.raises(ValueError, match="round 1"):
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            findings,
            MultiAgentRunStatus.COMPLETED,
        )


def test_duplicate_sdk_finding_id_raises_value_error():
    findings = [
        _sdk_finding("duplicate", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding("duplicate", AgentName.METRIC, CauseType.TRAFFIC_SPIKE),
    ]

    with pytest.raises(ValueError, match="Duplicate SDK finding id"):
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            findings,
            MultiAgentRunStatus.COMPLETED,
        )


def test_sdk_revision_cannot_target_custom_finding():
    custom = _finding(
        "custom-original",
        AgentName.LOG,
        AgentFindingType.ROOT_CAUSE,
        CauseType.TRAFFIC_SPIKE,
        0.8,
        ["ev-custom-original"],
        "Custom finding",
    )
    revision = _sdk_finding(
        "sdk-revision",
        AgentName.LOG,
        CauseType.TRAFFIC_SPIKE,
        analysis_round=2,
        revises_finding_id="custom-original",
    )

    with pytest.raises(ValueError, match="SDK round 1"):
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [custom, revision],
            MultiAgentRunStatus.COMPLETED,
        )


def test_custom_duplicate_id_does_not_override_sdk_revision_target():
    sdk_original = _sdk_finding(
        "original", AgentName.LOG, CauseType.DATABASE_SLOWDOWN
    )
    custom_same_id = _finding(
        "original",
        AgentName.METRIC,
        AgentFindingType.ROOT_CAUSE,
        CauseType.DATABASE_SLOWDOWN,
        0.8,
        ["ev-custom"],
        "Custom duplicate id",
    )
    revision = _sdk_finding(
        "revision",
        AgentName.LOG,
        CauseType.TRAFFIC_SPIKE,
        analysis_round=2,
        revises_finding_id="original",
    )

    assert decide_hybrid_status(
        _hypothesis(CauseType.UNKNOWN, 0.4),
        [sdk_original, custom_same_id, revision],
        MultiAgentRunStatus.COMPLETED,
    ) == CoordinationDecisionStatus.FALLBACK


def test_conflicting_agent_names_returns_only_involved_specialists():
    baseline = _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8)
    findings = [_sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE)]

    assert conflicting_agent_names(baseline, findings) == {AgentName.LOG}


def test_conflicting_agent_names_includes_both_differing_specialists():
    findings = [
        _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding("metric", AgentName.METRIC, CauseType.DATABASE_SLOWDOWN),
    ]

    assert conflicting_agent_names(
        _hypothesis(CauseType.UNKNOWN, 0.4), findings
    ) == {AgentName.LOG, AgentName.METRIC}


def test_conflicting_agent_names_includes_contradiction_author_and_cause_owner():
    findings = [
        _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "metric",
            AgentName.METRIC,
            CauseType.TRAFFIC_SPIKE,
            finding_type=AgentFindingType.CONTRADICTION,
        ),
    ]

    assert conflicting_agent_names(
        _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8), findings
    ) == {AgentName.LOG, AgentName.METRIC}


def test_low_confidence_baseline_still_returns_single_cause_contradiction_parties():
    findings = [
        _sdk_finding("log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "metric",
            AgentName.METRIC,
            CauseType.TRAFFIC_SPIKE,
            finding_type=AgentFindingType.CONTRADICTION,
        ),
    ]

    assert conflicting_agent_names(
        _hypothesis(CauseType.UNKNOWN, 0.4), findings
    ) == {AgentName.LOG, AgentName.METRIC}


def test_hybrid_builder_sets_code_owned_fields_and_ignores_v5_findings():
    sdk = _sdk_finding("sdk", AgentName.LOG, CauseType.TRAFFIC_SPIKE)
    v5 = _finding(
        "v5",
        AgentName.METRIC,
        AgentFindingType.ROOT_CAUSE,
        CauseType.DATABASE_SLOWDOWN,
        0.9,
        ["ev-v5"],
        "V5 rule finding",
    )

    review = build_hybrid_coordination_review(
        "inv-1",
        [sdk, v5],
        [_evidence("ev-sdk"), _evidence("ev-v5")],
        [_hypothesis(CauseType.UNKNOWN, 0.4)],
        MultiAgentRunStatus.PARTIAL,
        "Model summary",
        "Model uncertainty",
    )

    assert review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    assert review.run_status == MultiAgentRunStatus.PARTIAL
    assert review.decision_status == CoordinationDecisionStatus.FALLBACK
    assert review.baseline_cause_type == CauseType.UNKNOWN
    assert review.selected_cause_type == CauseType.UNKNOWN
    assert review.summary == "Model summary"
    assert review.uncertainty == "Model uncertainty"
    assert review.candidates[0].cause_type == review.selected_cause_type
    traffic_candidate = next(
        candidate
        for candidate in review.candidates
        if candidate.cause_type == CauseType.TRAFFIC_SPIKE
    )
    assert traffic_candidate.supporting_finding_ids == ["sdk"]
    assert all(
        "v5" not in candidate.supporting_finding_ids
        for candidate in review.candidates
    )


def test_hybrid_contradiction_only_candidate_keeps_finding_and_evidence_ids():
    contradiction = _sdk_finding(
        "metric-contradiction",
        AgentName.METRIC,
        CauseType.DEPLOYMENT_REGRESSION,
        finding_type=AgentFindingType.CONTRADICTION,
    )

    review = build_hybrid_coordination_review(
        "inv-1",
        [contradiction],
        [_evidence("ev-metric-contradiction")],
        [_hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8)],
        MultiAgentRunStatus.COMPLETED,
        "Contradicted baseline",
        "Needs review",
    )

    assert review.decision_status == CoordinationDecisionStatus.CONFLICT
    assert review.selected_cause_type is None
    assert review.candidates[0].contradicting_finding_ids == ["metric-contradiction"]
    assert review.candidates[0].contradicting_evidence_ids == [
        "ev-metric-contradiction"
    ]


def test_hybrid_keeps_alternative_and_contradicted_baseline_candidates_visible():
    findings = [
        _sdk_finding("log-root", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
        _sdk_finding(
            "metric-contradiction",
            AgentName.METRIC,
            CauseType.DEPLOYMENT_REGRESSION,
            finding_type=AgentFindingType.CONTRADICTION,
        ),
    ]

    review = build_hybrid_coordination_review(
        "inv-1",
        findings,
        [_evidence("ev-log-root"), _evidence("ev-metric-contradiction")],
        [_hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8)],
        MultiAgentRunStatus.COMPLETED,
        "Conflicting review",
        "Needs human confirmation",
    )

    assert review.decision_status == CoordinationDecisionStatus.CONFLICT
    assert review.selected_cause_type is None
    assert [candidate.cause_type for candidate in review.candidates] == [
        CauseType.TRAFFIC_SPIKE,
        CauseType.DEPLOYMENT_REGRESSION,
    ]
    assert [candidate.rank for candidate in review.candidates] == [1, 2]
    baseline_candidate = review.candidates[1]
    assert baseline_candidate.contradicting_finding_ids == ["metric-contradiction"]
    assert baseline_candidate.contradicting_evidence_ids == [
        "ev-metric-contradiction"
    ]


@pytest.mark.parametrize(
    ("baseline", "findings", "expected_status", "expected_selected"),
    [
        (
            _hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8),
            [_sdk_finding("conflict", AgentName.LOG, CauseType.TRAFFIC_SPIKE)],
            CoordinationDecisionStatus.CONFLICT,
            None,
        ),
        (
            _hypothesis(CauseType.UNKNOWN, 0.4),
            [
                _sdk_finding("lead-log", AgentName.LOG, CauseType.TRAFFIC_SPIKE),
                _sdk_finding("lead-metric", AgentName.METRIC, CauseType.TRAFFIC_SPIKE),
            ],
            CoordinationDecisionStatus.AGENT_LEADS,
            CauseType.TRAFFIC_SPIKE,
        ),
    ],
)
def test_hybrid_builder_selected_cause_follows_deterministic_decision(
    baseline, findings, expected_status, expected_selected
):
    review = build_hybrid_coordination_review(
        "inv-1",
        findings,
        [_evidence(item_id) for finding in findings for item_id in finding.evidence_ids],
        [baseline],
        MultiAgentRunStatus.COMPLETED,
        "Summary",
        "Uncertainty",
    )

    assert review.decision_status == expected_status
    assert review.selected_cause_type == expected_selected


def test_agent_leads_selected_cause_is_ranked_before_high_confidence_signal():
    findings = [
        _sdk_finding(
            "signal",
            AgentName.DEPLOYMENT,
            CauseType.DATABASE_SLOWDOWN,
            confidence=0.99,
            finding_type=AgentFindingType.SIGNAL,
        ),
        _sdk_finding(
            "log-root", AgentName.LOG, CauseType.TRAFFIC_SPIKE, confidence=0.6
        ),
        _sdk_finding(
            "metric-root", AgentName.METRIC, CauseType.TRAFFIC_SPIKE, confidence=0.6
        ),
    ]

    review = build_hybrid_coordination_review(
        "inv-1",
        findings,
        [_evidence(item_id) for finding in findings for item_id in finding.evidence_ids],
        [_hypothesis(CauseType.UNKNOWN, 0.4)],
        MultiAgentRunStatus.COMPLETED,
        "Agent-led review",
        "Deterministic baseline is weak",
    )

    assert review.decision_status == CoordinationDecisionStatus.AGENT_LEADS
    assert review.selected_cause_type == CauseType.TRAFFIC_SPIKE
    assert review.candidates[0].cause_type == review.selected_cause_type
    assert [candidate.rank for candidate in review.candidates] == [1, 2]


def test_hybrid_builder_rejects_finding_from_another_investigation():
    finding = _sdk_finding(
        "foreign",
        AgentName.LOG,
        CauseType.TRAFFIC_SPIKE,
        investigation_id="inv-2",
    )

    with pytest.raises(ValueError, match="investigation"):
        build_hybrid_coordination_review(
            "inv-1",
            [finding],
            [_evidence("ev-foreign")],
            [],
            MultiAgentRunStatus.COMPLETED,
            "",
            "",
        )


def _finding(
    finding_id: str,
    agent_name: AgentName,
    finding_type: AgentFindingType,
    cause_type: CauseType,
    confidence: float,
    evidence_ids: list[str],
    summary: str,
) -> AgentFinding:
    return AgentFinding(
        id=finding_id,
        investigation_id="inv-1",
        agent_name=agent_name,
        finding_type=finding_type,
        summary=summary,
        confidence=confidence,
        evidence_ids=evidence_ids,
        related_cause_type=cause_type,
    )


def _evidence(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=f"{evidence_id} summary",
    )
