from datetime import datetime, timedelta

import pytest

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def _analyze_case(case_id: str):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    return RcaAnalyzer().analyze(event, evidence)


def _event(**overrides):
    values = {
        "source": IncidentSource.SIMULATED,
        "service": "test-service",
        "environment": "prod",
        "severity": Severity.WARNING,
        "title": "Latency increased",
        "description": "The service is unhealthy",
        "started_at": datetime.fromisoformat("2026-07-03T12:00:00+08:00"),
        "time_window_minutes": 30,
        "signals": {},
    }
    values.update(overrides)
    return IncidentEvent(**values)


def _metric_evidence(payload: dict):
    return EvidenceItem(
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=datetime.fromisoformat("2026-07-03T12:01:00+08:00"),
        summary="Metric trend sample",
        payload=payload,
    )


def _evidence(
    evidence_id: str,
    provider: EvidenceProvider,
    kind: EvidenceKind,
    payload: dict,
    *,
    minutes: int = 1,
):
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kind,
        timestamp=datetime.fromisoformat("2026-07-03T12:00:00+08:00")
        + timedelta(minutes=minutes),
        summary="Canonical signal",
        payload=payload,
    )


def test_deployment_regression_ranked_first():
    hypotheses = _analyze_case("deployment_regression")

    assert hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert hypotheses[0].confidence >= 0.8


def test_traffic_spike_ranked_first():
    hypotheses = _analyze_case("traffic_spike")

    assert hypotheses[0].cause_type == CauseType.TRAFFIC_SPIKE


def test_dependency_timeout_ranked_first():
    hypotheses = _analyze_case("dependency_timeout")

    assert hypotheses[0].cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE


def test_database_slowdown_ranked_first():
    hypotheses = _analyze_case("database_slowdown")

    assert hypotheses[0].cause_type == CauseType.DATABASE_SLOWDOWN


def test_single_bad_instance_ranked_first():
    hypotheses = _analyze_case("single_bad_instance")

    assert hypotheses[0].cause_type == CauseType.SINGLE_INSTANCE_ISSUE


def test_unknown_fallback_for_empty_evidence():
    hypotheses = RcaAnalyzer().analyze(_event(), [])

    assert len(hypotheses) == 1
    assert hypotheses[0].cause_type == CauseType.UNKNOWN
    assert hypotheses[0].confidence == 0.2


def test_small_qps_change_does_not_trigger_traffic_or_deployment():
    event = _event(signals={"qps": "normal"})
    evidence = [_metric_evidence({"qps_change": "+3%", "error_rate": "12%"})]

    hypotheses = RcaAnalyzer().analyze(event, evidence)

    assert hypotheses[0].cause_type == CauseType.UNKNOWN
    assert CauseType.TRAFFIC_SPIKE not in {
        hypothesis.cause_type for hypothesis in hypotheses
    }
    assert CauseType.DEPLOYMENT_REGRESSION not in {
        hypothesis.cause_type for hypothesis in hypotheses
    }


def test_resource_drop_does_not_trigger_saturation():
    evidence = [
        _metric_evidence(
            {
                "signal_type": "cpu",
                "change_percent": -100,
                "deviation_score": 2,
            }
        )
    ]

    hypotheses = RcaAnalyzer().analyze(_event(), evidence)

    assert hypotheses[0].cause_type == CauseType.UNKNOWN


def test_timeout_log_outranks_incidental_traffic_increase():
    evidence = [
        _metric_evidence(
            {
                "signal_type": "traffic",
                "change_percent": 900,
                "deviation_score": 18,
            }
        ),
        _evidence(
            "ev-timeout",
            EvidenceProvider.LOG,
            EvidenceKind.LOG_PATTERN,
            {
                "component": "checkout-service",
                "dependency": "payment-service",
                "signal_type": "timeout",
                "exception": "TimeoutException",
            },
        ),
    ]

    hypotheses = RcaAnalyzer().analyze(_event(), evidence)

    assert hypotheses[0].cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE


def test_supporting_evidence_ids_belong_to_input_evidence():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)

    hypotheses = RcaAnalyzer().analyze(event, evidence)

    evidence_ids = {item.id for item in evidence}
    for hypothesis in hypotheses:
        assert set(hypothesis.supporting_evidence_ids) <= evidence_ids


def test_confidence_bounds_and_sorted_order():
    hypotheses = _analyze_case("deployment_regression")

    confidences = [hypothesis.confidence for hypothesis in hypotheses]
    assert all(0 <= confidence <= 1 for confidence in confidences)
    assert confidences == sorted(confidences, reverse=True)


def test_deployment_detection_uses_structured_fields_when_summary_is_neutral():
    event = load_incident_case("deployment_regression")
    evidence = [
        item.model_copy(update={"summary": "Observed signal in incident window"})
        for item in build_mock_provider_registry().collect_all(event)
    ]

    hypotheses = RcaAnalyzer().analyze(event, evidence)

    assert hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION


def test_failed_evidence_cannot_support_concrete_cause():
    event = _event(signals={"qps": "high"})
    evidence = [
        _metric_evidence({"qps_change": "+90%"}).model_copy(
            update={"status": EvidenceStatus.FAILED}
        )
    ]

    hypotheses = RcaAnalyzer().analyze(event, evidence)

    assert hypotheses[0].cause_type == CauseType.UNKNOWN


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            [
                _evidence(
                    "ev-traffic",
                    EvidenceProvider.METRIC,
                    EvidenceKind.METRIC_TREND,
                    {
                        "component": "checkout",
                        "signal_type": "traffic",
                        "signal_name": "qps",
                        "change_percent": 80,
                        "deviation_score": 2,
                    },
                )
            ],
            CauseType.TRAFFIC_SPIKE,
        ),
        (
            [
                _evidence(
                    "ev-timeout",
                    EvidenceProvider.LOG,
                    EvidenceKind.LOG_PATTERN,
                    {
                        "component": "checkout",
                        "dependency": "payment",
                        "signal_type": "timeout",
                        "signal_name": "TimeoutException",
                    },
                )
            ],
            CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
        ),
        (
            [
                _evidence(
                    "ev-cpu",
                    EvidenceProvider.METRIC,
                    EvidenceKind.METRIC_TREND,
                    {
                        "component": "checkout",
                        "signal_type": "cpu",
                        "signal_name": "cpu_usage",
                        "deviation_score": 2,
                    },
                )
            ],
            CauseType.RESOURCE_SATURATION,
        ),
        (
            [
                _evidence(
                    "ev-network",
                    EvidenceProvider.METRIC,
                    EvidenceKind.METRIC_TREND,
                    {
                        "component": "checkout",
                        "node": "node-a",
                        "signal_type": "network_corruption",
                        "signal_name": "packet_loss",
                        "deviation_score": 2,
                    },
                )
            ],
            CauseType.NETWORK_FAULT,
        ),
        (
            [
                _evidence(
                    "ev-process",
                    EvidenceProvider.LOG,
                    EvidenceKind.LOG_PATTERN,
                    {
                        "component": "checkout",
                        "instance": "checkout-1",
                        "signal_type": "process",
                        "signal_name": "container_restart",
                        "deviation_score": 1,
                    },
                )
            ],
            CauseType.PROCESS_OR_CONTAINER_FAILURE,
        ),
        (
            [
                _evidence(
                    "ev-database",
                    EvidenceProvider.METRIC,
                    EvidenceKind.METRIC_TREND,
                    {
                        "component": "database",
                        "db_p95": 2400.0,
                        "deviation_score": 2,
                    },
                )
            ],
            CauseType.DATABASE_SLOWDOWN,
        ),
        (
            [
                _evidence(
                    "ev-instance",
                    EvidenceProvider.METRIC,
                    EvidenceKind.METRIC_TREND,
                    {
                        "component": "checkout",
                        "instance": "checkout-1",
                        "signal_type": "cpu",
                        "signal_name": "cpu_usage",
                        "deviation_score": 2,
                    },
                )
            ],
            CauseType.SINGLE_INSTANCE_ISSUE,
        ),
    ],
)
def test_canonical_signals_select_general_rules(evidence, expected):
    hypotheses = RcaAnalyzer().analyze(_event(), evidence)

    assert hypotheses[0].cause_type == expected
    assert hypotheses[0].supporting_evidence_ids == [evidence[0].id]


def test_canonical_deployment_requires_error_after_deployment():
    evidence = [
        _evidence(
            "ev-deploy",
            EvidenceProvider.DEPLOY,
            EvidenceKind.DEPLOYMENT,
            {
                "service": "checkout",
                "component": "checkout",
                "signal_type": "deployment",
                "signal_name": "deployment",
            },
            minutes=1,
        ),
        _evidence(
            "ev-error",
            EvidenceProvider.LOG,
            EvidenceKind.LOG_PATTERN,
            {
                "component": "checkout",
                "signal_type": "error",
                "signal_name": "NullPointerException",
            },
            minutes=2,
        ),
    ]

    hypotheses = RcaAnalyzer().analyze(_event(), evidence)

    assert hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert set(hypotheses[0].supporting_evidence_ids) == {"ev-deploy", "ev-error"}


def test_hypothesis_order_is_stable_when_input_order_changes():
    evidence = [
        _evidence(
            "ev-traffic",
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            {
                "component": "checkout",
                "signal_type": "traffic",
                "change_percent": 80,
                "deviation_score": 2,
            },
        ),
        _evidence(
            "ev-cpu",
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            {
                "component": "checkout",
                "signal_type": "cpu",
                "deviation_score": 2,
            },
        ),
    ]

    forward = RcaAnalyzer().analyze(_event(), evidence)
    reverse = RcaAnalyzer().analyze(_event(), list(reversed(evidence)))

    assert [item.cause_type for item in forward] == [
        item.cause_type for item in reverse
    ]
