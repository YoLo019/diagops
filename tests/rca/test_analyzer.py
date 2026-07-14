from datetime import datetime

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
