import pytest

from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


@pytest.mark.parametrize(
    ("case_id", "expected_cause", "required_evidence"),
    [
        ("deployment_regression", CauseType.DEPLOYMENT_REGRESSION, "NullPointerException"),
        ("traffic_spike", CauseType.TRAFFIC_SPIKE, "QPS increased sharply"),
        (
            "dependency_timeout",
            CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            "inventory-service latency increased",
        ),
        ("database_slowdown", CauseType.DATABASE_SLOWDOWN, "Database query latency"),
        ("single_bad_instance", CauseType.SINGLE_INSTANCE_ISSUE, "One instance has high CPU"),
    ],
)
def test_golden_case_primary_cause_and_report_evidence(
    case_id,
    expected_cause,
    required_evidence,
):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    report = ReportGenerator().generate(f"inv-{case_id}", event, evidence, hypotheses)

    assert hypotheses[0].cause_type == expected_cause
    assert required_evidence in report.markdown
    assert "事实与推断" in report.markdown
