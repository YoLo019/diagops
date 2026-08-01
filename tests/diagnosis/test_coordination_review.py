from datetime import UTC, datetime, timedelta

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.coordination_review import (
    LOW_CONFIDENCE,
    build_coordination_review,
    build_root_cause_attributions,
    conflicting_agent_names,
    decide_hybrid_status,
)
from backend.diagnosis.coordination_review import (
    build_hybrid_coordination_review as _build_hybrid_coordination_review,
)
from backend.domain.agent_findings import AgentFinding, AgentFindingType, AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ModelProvider,
    MultiAgentRunStatus,
    StabilizationCategory,
)


def build_hybrid_coordination_review(*args, **kwargs):
    kwargs.setdefault("model_provider", ModelProvider.OPENAI)
    kwargs.setdefault("model_name", "gpt-test")
    return _build_hybrid_coordination_review(*args, **kwargs)


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


def test_deterministic_attribution_clusters_real_observation_times_and_maps_reason():
    start = datetime(2026, 7, 3, 12, tzinfo=UTC)
    evidence = [
        EvidenceItem(
            id=f"ev-network-{index}",
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=start + timedelta(seconds=offset),
            summary="Network signal",
            payload={
                "component": "checkout",
                "node": "node-a",
                "signal_type": "network_corruption",
                "signal_name": "packet_loss",
                "deviation_score": score,
            },
        )
        for index, (offset, score) in enumerate(
            [(60, 1.2), (120, 2.0), (180, 1.5), (600, 3.0)]
        )
    ]
    hypothesis = Hypothesis(
        cause_type=CauseType.NETWORK_FAULT,
        summary="Network fault",
        confidence=0.8,
        supporting_evidence_ids=[item.id for item in evidence],
    )

    review = build_coordination_review("inv-1", [], evidence, [hypothesis])

    assert len(review.root_causes) == 2
    # F17：review 层顺序与 attribution 构建层一致；全 legacy cohort 按 cluster
    # score 降序（600s 单点 cluster score 6 先于 60s 三点 cluster score 5）。
    assert [item.root_cause_occurred_at for item in review.root_causes] == [
        start + timedelta(seconds=600),
        start + timedelta(seconds=60),
    ]
    assert all(item.root_cause_component == "node-a" for item in review.root_causes)
    assert all(
        item.root_cause_reason == "network packet corruption"
        for item in review.root_causes
    )
    assert set(review.root_causes[1].supporting_evidence_ids) == {
        "ev-network-0",
        "ev-network-1",
        "ev-network-2",
    }


def test_attribution_never_merges_evidence_across_anomaly_segments():
    start = datetime(2026, 7, 3, 12, tzinfo=UTC)
    evidence = [
        EvidenceItem(
            id=f"ev-seg-{segment}-{index}",
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=start + timedelta(seconds=offset),
            summary="Memory signal",
            payload={
                "component": "checkout",
                "signal_type": "memory",
                "signal_name": "memory_usage",
                "deviation_score": score,
                "anomaly_segment_id": segment,
                "anomaly_onset": (start + timedelta(seconds=onset)).isoformat(),
            },
        )
        for segment, rows in (
            ("seg-a", [(0, 1.0), (30, 2.0)]),
            ("seg-b", [(60, 1.5), (90, 3.0)]),
        )
        for index, (offset, score) in enumerate(rows)
        for onset in [rows[0][0]]
    ]
    hypothesis = Hypothesis(
        cause_type=CauseType.RESOURCE_SATURATION,
        summary="Memory saturation",
        confidence=0.8,
        supporting_evidence_ids=[item.id for item in evidence],
    )

    review = build_coordination_review("inv-1", [], evidence, [hypothesis])

    # 两条 segment 间隔仅 30s，时间聚类会合并；segment 聚类必须保持分离。
    assert len(review.root_causes) == 2
    by_ids = {
        frozenset(item.supporting_evidence_ids): item for item in review.root_causes
    }
    seg_a = by_ids[frozenset({"ev-seg-seg-a-0", "ev-seg-seg-a-1"})]
    seg_b = by_ids[frozenset({"ev-seg-seg-b-0", "ev-seg-seg-b-1"})]
    assert seg_a.root_cause_occurred_at == start
    assert seg_b.root_cause_occurred_at == start + timedelta(seconds=60)


def test_attribution_keeps_historical_evidence_on_time_cluster_fallback():
    start = datetime(2026, 7, 3, 12, tzinfo=UTC)
    segmented = EvidenceItem(
        id="ev-segmented",
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=start,
        summary="Segmented memory signal",
        payload={
            "component": "checkout",
            "signal_type": "memory",
            "signal_name": "memory_usage",
            "deviation_score": 2.0,
            "anomaly_segment_id": "seg-a",
            "anomaly_onset": start.isoformat(),
        },
    )
    historical = EvidenceItem(
        id="ev-historical",
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=start + timedelta(seconds=30),
        summary="Legacy memory signal without segment",
        payload={
            "component": "checkout",
            "signal_type": "memory",
            "signal_name": "memory_usage",
            "deviation_score": 1.0,
        },
    )
    hypothesis = Hypothesis(
        cause_type=CauseType.RESOURCE_SATURATION,
        summary="Memory saturation",
        confidence=0.8,
        supporting_evidence_ids=[segmented.id, historical.id],
    )

    review = build_coordination_review("inv-1", [], [segmented, historical], [hypothesis])

    # 有 segment 的证据与无 segment 的历史证据不得跨路径合并。
    assert len(review.root_causes) == 2
    by_ids = {
        tuple(item.supporting_evidence_ids): item for item in review.root_causes
    }
    assert by_ids[("ev-segmented",)].root_cause_occurred_at == start
    assert by_ids[("ev-historical",)].root_cause_occurred_at == start + timedelta(
        seconds=30
    )


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


def test_valid_baseline_accepts_single_evidenced_root_cause_corroboration():
    finding = _sdk_finding(
        "metric",
        AgentName.METRIC,
        CauseType.DATABASE_SLOWDOWN,
    )

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.DATABASE_SLOWDOWN, 0.8),
            [finding],
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.AGREEMENT
    )


@pytest.mark.parametrize(
    "finding",
    [
        _sdk_finding(
            "no-evidence",
            AgentName.METRIC,
            CauseType.DATABASE_SLOWDOWN,
            finding_type=AgentFindingType.SIGNAL,
        ).model_copy(update={"evidence_ids": []}),
        _sdk_finding(
            "low-confidence",
            AgentName.METRIC,
            CauseType.DATABASE_SLOWDOWN,
            confidence=LOW_CONFIDENCE - 0.01,
            finding_type=AgentFindingType.SIGNAL,
        ),
    ],
    ids=["no-evidence", "low-confidence"],
)
def test_single_signal_without_qualified_support_falls_back(finding):
    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.DATABASE_SLOWDOWN, 0.8),
            [finding],
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.FALLBACK
    )


@pytest.mark.parametrize("agent_names", [[AgentName.METRIC], list(AgentName)])
def test_unknown_baseline_does_not_promote_signals_to_agent_leads(agent_names):
    signals = [
        _sdk_finding(
            f"signal-{agent_name.value}",
            agent_name,
            CauseType.DATABASE_SLOWDOWN,
            finding_type=AgentFindingType.SIGNAL,
        )
        for agent_name in agent_names
    ]
    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.UNKNOWN, 0.4),
            signals,
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.FALLBACK
    )


def test_different_evidenced_signal_does_not_conflict_with_valid_baseline():
    signal = _sdk_finding(
        "metric",
        AgentName.METRIC,
        CauseType.TRAFFIC_SPIKE,
        finding_type=AgentFindingType.SIGNAL,
    )

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.DATABASE_SLOWDOWN, 0.8),
            [signal],
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.FALLBACK
    )


def test_partial_run_cannot_use_single_corroboration():
    finding = _sdk_finding(
        "metric", AgentName.METRIC, CauseType.DATABASE_SLOWDOWN
    )

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.DATABASE_SLOWDOWN, 0.8),
            [finding],
            MultiAgentRunStatus.PARTIAL,
        )
        == CoordinationDecisionStatus.FALLBACK
    )


def test_baseline_contradiction_blocks_single_corroboration():
    findings = [
        _sdk_finding("metric", AgentName.METRIC, CauseType.DATABASE_SLOWDOWN),
        _sdk_finding(
            "log",
            AgentName.LOG,
            CauseType.DATABASE_SLOWDOWN,
            finding_type=AgentFindingType.CONTRADICTION,
        ),
    ]

    assert (
        decide_hybrid_status(
            _hypothesis(CauseType.DATABASE_SLOWDOWN, 0.8),
            findings,
            MultiAgentRunStatus.COMPLETED,
        )
        == CoordinationDecisionStatus.CONFLICT
    )


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
        [_evidence("ev-sdk", CauseType.TRAFFIC_SPIKE), _evidence("ev-v5")],
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


def test_hybrid_builder_requires_and_persists_attribution():
    finding = _sdk_finding("sdk", AgentName.LOG, CauseType.TRAFFIC_SPIKE)
    args = (
        "inv-1",
        [finding],
        [_evidence("ev-sdk", CauseType.TRAFFIC_SPIKE)],
        [_hypothesis(CauseType.DEPLOYMENT_REGRESSION, 0.8)],
        MultiAgentRunStatus.COMPLETED,
        "Summary",
        "Uncertainty",
    )

    with pytest.raises(TypeError):
        _build_hybrid_coordination_review(*args)

    attributed_review = build_hybrid_coordination_review(
        *args,
        model_provider=ModelProvider.OPENAI,
        model_name="gpt-test",
        primary_stabilization_category=StabilizationCategory.HYBRID_CONTRACT,
        secondary_stabilization_categories=[StabilizationCategory.FINAL_SYNTHESIS],
    )

    assert attributed_review.model_provider == ModelProvider.OPENAI
    assert attributed_review.model_name == "gpt-test"
    assert (
        attributed_review.primary_stabilization_category
        == StabilizationCategory.HYBRID_CONTRACT
    )
    assert attributed_review.secondary_stabilization_categories == [
        StabilizationCategory.FINAL_SYNTHESIS
    ]


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
        [
            _evidence("ev-log-root", CauseType.TRAFFIC_SPIKE),
            _evidence("ev-metric-contradiction"),
        ],
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
        [
            _evidence(item_id, finding.related_cause_type)
            for finding in findings
            for item_id in finding.evidence_ids
        ],
        [baseline],
        MultiAgentRunStatus.COMPLETED,
        "Summary",
        "Uncertainty",
    )

    assert review.decision_status == expected_status
    assert review.selected_cause_type == expected_selected


def test_agent_leads_excludes_high_confidence_signal_from_candidates():
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
        [
            _evidence(item_id, finding.related_cause_type)
            for finding in findings
            for item_id in finding.evidence_ids
        ],
        [_hypothesis(CauseType.UNKNOWN, 0.4)],
        MultiAgentRunStatus.COMPLETED,
        "Agent-led review",
        "Deterministic baseline is weak",
    )

    assert review.decision_status == CoordinationDecisionStatus.AGENT_LEADS
    assert review.selected_cause_type == CauseType.TRAFFIC_SPIKE
    assert review.candidates[0].cause_type == review.selected_cause_type
    assert [candidate.rank for candidate in review.candidates] == [1]


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


# --- basis-aware Attribution（V10.1 T5, spec §8.3） ---------------------------

ATTRIBUTION_BASE = datetime(2026, 7, 3, 12, tzinfo=UTC)


def _metric_signal_evidence(
    evidence_id: str,
    offset_seconds: float,
    deviation: float,
    *,
    component: str = "checkout",
    signal_type: str = "memory",
    segment_id: str | None = None,
    strength_basis: object = None,
    point_count: object = None,
) -> EvidenceItem:
    payload: dict = {
        "component": component,
        "signal_type": signal_type,
        "signal_name": f"{signal_type}_metric",
        "deviation_score": deviation,
    }
    if segment_id is not None:
        payload["anomaly_segment_id"] = segment_id
    if strength_basis is not None:
        payload["strength_basis"] = strength_basis
    if point_count is not None:
        payload["anomaly_point_count"] = point_count
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=ATTRIBUTION_BASE + timedelta(seconds=offset_seconds),
        summary=f"{evidence_id} summary",
        payload=payload,
    )


def _saturation_hypothesis(evidence: list[EvidenceItem]) -> Hypothesis:
    return Hypothesis(
        cause_type=CauseType.RESOURCE_SATURATION,
        summary="Resource saturation",
        confidence=0.8,
        supporting_evidence_ids=[item.id for item in evidence],
    )


def _saturation_root_cause_ids(evidence: list[EvidenceItem]) -> list[list[str]]:
    # cohort 排序语义定义在 attribution 排名函数层；F17 起 domain validator 不再
    # 重排 root_causes，review 层顺序与本函数输出一致（见下方 consumer 合同测试）。
    attributions = build_root_cause_attributions(
        evidence, [_saturation_hypothesis(evidence)]
    )
    return [item.supporting_evidence_ids for item in attributions]


def test_attribution_all_legacy_clusters_keep_v10_score_order():
    evidence = [
        _metric_signal_evidence("ev-weak", 0, 1.0, segment_id="seg-a"),
        _metric_signal_evidence("ev-strong", 60, 5.0, segment_id="seg-b"),
    ]

    # V10 排名：cluster score 高者优先（8 先于 4），与 onset 先后无关。
    assert _saturation_root_cause_ids(evidence) == [["ev-strong"], ["ev-weak"]]


def test_attribution_relative_cohort_keeps_cluster_score_order():
    evidence = [
        _metric_signal_evidence(
            "ev-weak", 0, 1.0, segment_id="seg-a",
            strength_basis="relative", point_count=1,
        ),
        _metric_signal_evidence(
            "ev-strong", 60, 5.0, segment_id="seg-b",
            strength_basis="relative", point_count=1,
        ),
    ]

    assert _saturation_root_cause_ids(evidence) == [["ev-strong"], ["ev-weak"]]


def test_attribution_presence_cohort_orders_by_support_tuple_not_onset():
    evidence = [
        _metric_signal_evidence(
            "ev-small", 0, 1.0, segment_id="seg-a",
            strength_basis="presence_only", point_count=1,
        ),
        *[
            _metric_signal_evidence(
                f"ev-big-{index}", 100 + 30 * index, 1.0, segment_id="seg-b",
                strength_basis="presence_only", point_count=count,
            )
            for index, count in enumerate([3, 2, 1])
        ],
    ]

    # 总 point count 6 的 cluster 优先于 onset 更早的单点 cluster；
    # presence 数值 1 不参与幅度比较（spec F6）。
    assert _saturation_root_cause_ids(evidence) == [
        ["ev-big-0", "ev-big-1", "ev-big-2"],
        ["ev-small"],
    ]


def test_attribution_cross_cohort_compares_support_not_strength():
    # F6：relative 噪声（deviation 10、单点）不得压住 presence 真实信号（5 点支持）。
    evidence = [
        _metric_signal_evidence(
            "ev-noise", 0, 10.0, segment_id="seg-a",
            strength_basis="relative", point_count=1,
        ),
        _metric_signal_evidence(
            "ev-real", 100, 1.0, segment_id="seg-b",
            strength_basis="presence_only", point_count=5,
        ),
    ]

    assert _saturation_root_cause_ids(evidence) == [["ev-real"], ["ev-noise"]]


def test_attribution_mixed_cohort_orders_by_support_tuple():
    evidence = [
        _metric_signal_evidence(
            "ev-mixed-rel", 0, 2.0, segment_id="seg-a",
            strength_basis="relative", point_count=1,
        ),
        _metric_signal_evidence(
            "ev-mixed-pres", 30, 1.0, segment_id="seg-a",
            strength_basis="presence_only", point_count=2,
        ),
        _metric_signal_evidence(
            "ev-relative", 100, 10.0, segment_id="seg-b",
            strength_basis="relative", point_count=1,
        ),
    ]

    # mixed cluster（relative+presence）归 presence_or_mixed cohort，
    # support tuple (3,2,1,1) 优于纯 relative 单点 (1,1,1,1)。
    assert _saturation_root_cause_ids(evidence) == [
        ["ev-mixed-rel", "ev-mixed-pres"],
        ["ev-relative"],
    ]


def test_attribution_same_support_tuple_falls_back_to_onset():
    evidence = [
        _metric_signal_evidence(
            "ev-late", 100, 1.0, segment_id="seg-b",
            strength_basis="presence_only", point_count=2,
        ),
        _metric_signal_evidence(
            "ev-early", 0, 1.0, segment_id="seg-a",
            strength_basis="presence_only", point_count=2,
        ),
    ]

    assert _saturation_root_cause_ids(evidence) == [["ev-early"], ["ev-late"]]


def test_attribution_same_support_tuple_and_onset_falls_back_to_component():
    evidence = [
        _metric_signal_evidence(
            "ev-b", 0, 1.0, component="node-b", segment_id="seg-a",
            strength_basis="presence_only", point_count=2,
        ),
        _metric_signal_evidence(
            "ev-a", 0, 1.0, component="node-a", segment_id="seg-b",
            strength_basis="presence_only", point_count=2,
        ),
    ]

    assert _saturation_root_cause_ids(evidence) == [["ev-a"], ["ev-b"]]


def test_attribution_same_support_tuple_component_falls_back_to_reason():
    evidence = [
        _metric_signal_evidence(
            "ev-mem", 0, 1.0, signal_type="memory", segment_id="seg-a",
            strength_basis="presence_only", point_count=2,
        ),
        _metric_signal_evidence(
            "ev-cpu", 0, 1.0, signal_type="cpu", segment_id="seg-b",
            strength_basis="presence_only", point_count=2,
        ),
    ]

    # reason "container cpu load" 字典序先于 "container memory load"。
    assert _saturation_root_cause_ids(evidence) == [["ev-cpu"], ["ev-mem"]]


def test_attribution_malformed_quality_fields_derive_safe_defaults_without_writeback():
    weird = _metric_signal_evidence(
        "ev-weird", 0, 1.0, segment_id="seg-a",
        strength_basis="sometimes", point_count=-5,
    )
    legacy = _metric_signal_evidence("ev-legacy", 60, 2.0, segment_id="seg-b")

    # 非法 basis/point count 派生 legacy 与默认 1；两者同 cohort 按 V10 cluster score。
    assert _saturation_root_cause_ids([weird, legacy]) == [
        ["ev-legacy"],
        ["ev-weird"],
    ]
    assert weird.payload["strength_basis"] == "sometimes"
    assert weird.payload["anomaly_point_count"] == -5


@pytest.mark.parametrize("bad_deviation", [float("inf"), float("nan")])
def test_attribution_non_finite_deviation_derives_zero_without_outranking_finite(
    bad_deviation: float,
):
    # RA2-1：锚定 _deviation 的 math.isfinite 硬化分支。EvidenceItem 域校验在
    # 构建时拒绝非有限 payload，此处模拟绕过校验的内存态 payload（原地改 dict）：
    # inf/NaN deviation 派生安全默认 0、不写回 payload；该 cluster 不得因非
    # 有限值排到有限真实信号（deviation 5）之前。
    weird = _metric_signal_evidence("ev-weird", 0, 1.0, segment_id="seg-a")
    weird.payload["deviation_score"] = bad_deviation
    real = _metric_signal_evidence("ev-real", 60, 5.0, segment_id="seg-b")

    assert _saturation_root_cause_ids([weird, real]) == [
        ["ev-real"],
        ["ev-weird"],
    ]
    assert weird.payload["deviation_score"] is bad_deviation


# --- F17 consumer 层顺序合同（V10.1 T6, spec §13 F17） ------------------------


def _f6_cross_cohort_inputs() -> tuple[list[EvidenceItem], Hypothesis]:
    """F6 竞争场景：relative 单点噪声（deviation 10、更早 onset）与 presence
    多点真实信号（strength 1.0、更晚 onset）；cohort 排序真实信号在前。"""
    noise = _metric_signal_evidence(
        "ev-noise", 0, 10.0, segment_id="seg-noise",
        strength_basis="relative", point_count=1,
    )
    real = _metric_signal_evidence(
        "ev-real", 100, 1.0, segment_id="seg-real",
        strength_basis="presence_only", point_count=3,
    )
    evidence = [noise, real]
    return evidence, _saturation_hypothesis(evidence)


def _review_root_cause_ids(review) -> list[list[str]]:
    return [item.supporting_evidence_ids for item in review.root_causes]


def test_review_root_causes_keep_attribution_order_at_construction():
    evidence, hypothesis = _f6_cross_cohort_inputs()
    expected = build_root_cause_attributions(evidence, [hypothesis])

    review = build_coordination_review("inv-1", [], evidence, [hypothesis])

    # F17：review 层不再按 onset 重排，顺序必须与 attribution 构建层一致；
    # presence 多点真实信号领先 relative 单点噪声，对 Top-N consumer 可观测。
    assert _review_root_cause_ids(review) == [
        item.supporting_evidence_ids for item in expected
    ]
    assert _review_root_cause_ids(review) == [["ev-real"], ["ev-noise"]]


def test_review_root_causes_keep_attribution_order_after_inmemory_reload():
    evidence, hypothesis = _f6_cross_cohort_inputs()
    review = build_coordination_review("inv-1", [], evidence, [hypothesis])
    repository = InMemoryInvestigationRepository()
    # save_multi_agent_result 经 model_validate 全量重建 review，触发 domain
    # validator；reload 后 cohort 排序不得被 onset 重排覆盖。
    repository.save_multi_agent_result("inv-1", [], [], review)

    reloaded = repository.get_coordination_review("inv-1")

    assert reloaded is not None
    assert _review_root_cause_ids(reloaded) == [["ev-real"], ["ev-noise"]]


def test_review_root_causes_keep_attribution_order_after_sqlite_reload(tmp_path):
    evidence, hypothesis = _f6_cross_cohort_inputs()
    review = build_coordination_review("inv-1", [], evidence, [hypothesis])
    engine = create_db_engine(f"sqlite:///{tmp_path / 'diagops-f17.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    # coordination_reviews 外键要求父 investigation 行存在。
    repository.save(
        InvestigationRecord(
            id="inv-1",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout",
                environment="prod",
                severity=Severity.WARNING,
                title="f17 sqlite reload",
                description="f17 sqlite reload",
                started_at=ATTRIBUTION_BASE,
            ),
        )
    )

    repository.save_coordination_review(review)
    reloaded = repository.get_coordination_review("inv-1")

    # SQLite reload 经 CoordinationReview(**payload) 全量重建；
    # validator 不得把 cohort 排序重排为 onset 排序。
    assert reloaded is not None
    assert _review_root_cause_ids(reloaded) == [["ev-real"], ["ev-noise"]]


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


def _evidence(
    evidence_id: str, cause_type: CauseType | None = None
) -> EvidenceItem:
    provider, kind = {
        CauseType.TRAFFIC_SPIKE: (
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
        ),
        CauseType.DATABASE_SLOWDOWN: (
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
        ),
        CauseType.SINGLE_INSTANCE_ISSUE: (
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
        ),
        CauseType.DOWNSTREAM_DEPENDENCY_FAILURE: (
            EvidenceProvider.DEPENDENCY,
            EvidenceKind.DEPENDENCY_HEALTH,
        ),
    }.get(cause_type, (EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN))
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kind,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=f"{evidence_id} summary",
    )
