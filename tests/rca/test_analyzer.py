from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def _analyze_case(case_id: str):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    return RcaAnalyzer().analyze(event, evidence)


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
