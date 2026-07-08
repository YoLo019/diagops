import pytest

from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def extract_section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    marker = f"## {heading}"

    for index, line in enumerate(lines):
        if line == marker:
            section_start = index + 1
            break
    else:
        raise AssertionError(f"missing markdown section: {heading}")

    section_lines: list[str] = []
    for line in lines[section_start:]:
        if line.startswith("## "):
            break
        section_lines.append(line)

    return "\n".join(section_lines).strip()


def find_evidence(
    evidence: list[EvidenceItem],
    *,
    provider: EvidenceProvider,
    kind: EvidenceKind,
    payload_key: str,
    payload_value: object,
) -> EvidenceItem:
    matches = [
        item
        for item in evidence
        if item.provider == provider
        and item.kind == kind
        and item.payload.get(payload_key) == payload_value
    ]

    assert len(matches) == 1
    return matches[0]


def build_v2_orchestrator() -> DiagnosisOrchestrator:
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=InMemoryInvestigationRepository(),
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )


@pytest.mark.parametrize(
    (
        "case_id",
        "expected_cause",
        "evidence_provider",
        "evidence_kind",
        "payload_key",
        "payload_value",
    ),
    [
        (
            "deployment_regression",
            CauseType.DEPLOYMENT_REGRESSION,
            EvidenceProvider.LOG,
            EvidenceKind.LOG_PATTERN,
            "exception",
            "NullPointerException",
        ),
        (
            "traffic_spike",
            CauseType.TRAFFIC_SPIKE,
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            "qps_change",
            "+260%",
        ),
        (
            "dependency_timeout",
            CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            EvidenceProvider.DEPENDENCY,
            EvidenceKind.DEPENDENCY_HEALTH,
            "dependency",
            "inventory-service",
        ),
        (
            "database_slowdown",
            CauseType.DATABASE_SLOWDOWN,
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            "db_p95",
            "2400ms",
        ),
        (
            "single_bad_instance",
            CauseType.SINGLE_INSTANCE_ISSUE,
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            "instance",
            "profile-service-3",
        ),
    ],
)
def test_golden_case_primary_cause_and_report_evidence(
    case_id,
    expected_cause,
    evidence_provider,
    evidence_kind,
    payload_key,
    payload_value,
):
    event = load_incident_case(case_id)
    record = build_v2_orchestrator().run(event)

    assert record.status == "completed"
    assert record.report is not None
    assert record.actions
    assert record.verification_suggestions

    evidence = record.evidence
    report = record.report
    top = record.hypotheses[0]
    report_top = report.hypotheses[0]
    expected_evidence = find_evidence(
        evidence,
        provider=evidence_provider,
        kind=evidence_kind,
        payload_key=payload_key,
        payload_value=payload_value,
    )
    supporting_section = extract_section(report.markdown, "支持该结论的证据")

    assert top.cause_type == expected_cause
    assert report_top == top
    assert expected_evidence.id in report_top.supporting_evidence_ids
    assert expected_evidence.summary in supporting_section
    assert report.summary == top.summary
    assert f"`{top.cause_type}`" in report.markdown
    assert top.summary in report.markdown
    assert f"{top.confidence:.2f}" in report.markdown
    assert report.action_ids == [action.id for action in record.actions]
    assert report.verification_suggestion_ids == [
        suggestion.id for suggestion in record.verification_suggestions
    ]
    assert all(action.supporting_evidence_ids for action in record.actions)
    evidence_ids = {item.id for item in evidence}
    for action in record.actions:
        assert set(action.supporting_evidence_ids) <= evidence_ids
        if action.risk_level in {"medium", "high"}:
            assert action.requires_approval is True
    assert "建议动作" in report.markdown
    assert "需要审批的动作" in report.markdown
    assert "验证建议" in report.markdown
    assert "V2 未执行该动作" in report.markdown
    assert "事实与推断" in report.markdown
    assert "观察到的事实" in report.markdown
    assert "推断结论/不确定性" in report.markdown


def test_golden_deployment_regression_v5_coordination_review_references_known_records():
    repository = InMemoryInvestigationRepository()
    providers = build_mock_provider_registry()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )

    record = orchestrator.run(load_incident_case("deployment_regression"))
    findings = repository.list_agent_findings(record.id)
    review = repository.get_coordination_review(record.id)

    assert review is not None
    assert review.candidates[0].cause_type == CauseType.DEPLOYMENT_REGRESSION

    evidence_ids = {item.id for item in record.evidence}
    finding_ids = {finding.id for finding in findings}
    for candidate in review.candidates:
        assert set(candidate.supporting_evidence_ids) <= evidence_ids
        assert set(candidate.contradicting_evidence_ids) <= evidence_ids
        assert set(candidate.supporting_finding_ids) <= finding_ids
        assert set(candidate.contradicting_finding_ids) <= finding_ids
