from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def test_report_generator_outputs_markdown_with_evidence():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert report.investigation_id == "inv-1"
    assert "最可能根因" in report.markdown
    assert "NullPointerException" in report.markdown
    assert "payment-service v1.8.2" in report.markdown
    assert "事实与推断" in report.markdown
