from datetime import datetime

import pytest

from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
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
    assert "观察到的事实" in report.markdown
    assert "推断结论/不确定性" in report.markdown


def test_report_generator_rejects_empty_hypotheses():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)

    with pytest.raises(ValueError, match="hypotheses must contain at least one item"):
        ReportGenerator().generate("inv-1", event, evidence, [])


def test_report_generator_rejects_missing_supporting_evidence_id():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-real", "ev-missing-support"],
        )
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-support"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_rejects_missing_contradicting_evidence_id():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-real"],
            contradicting_evidence_ids=["ev-missing-contradiction"],
        )
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-contradiction"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_orders_markdown_evidence_chain_by_timestamp():
    event = load_incident_case("deployment_regression")
    older = _evidence(
        "ev-older",
        "older evidence summary",
        timestamp="2026-07-03T13:55:00+08:00",
    )
    newer = _evidence(
        "ev-newer",
        "newer evidence summary",
        timestamp="2026-07-03T14:05:00+08:00",
    )
    hypotheses = [_hypothesis(supporting_evidence_ids=["ev-older"])]

    report = ReportGenerator().generate("inv-1", event, [newer, older], hypotheses)

    assert report.markdown.index("older evidence summary") < report.markdown.index(
        "newer evidence summary"
    )
    assert report.timeline == [
        {"time": older.timestamp.isoformat(), "event": older.summary},
        {"time": newer.timestamp.isoformat(), "event": newer.summary},
    ]


def test_report_generator_renders_alternative_hypotheses():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(supporting_evidence_ids=["ev-real"]),
        _hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="Traffic also increased around the incident.",
            confidence=0.42,
        ),
    ]

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert "其他可能假设" in report.markdown
    assert "traffic_spike" in report.markdown
    assert "Traffic also increased around the incident." in report.markdown
    assert "0.42" in report.markdown


def test_report_generator_renders_real_contradicting_evidence_only():
    event = load_incident_case("deployment_regression")
    supporting = _evidence("ev-support", "real supporting evidence")
    contradicting = _evidence("ev-contradicting", "real contradicting evidence")
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-support"],
            contradicting_evidence_ids=["ev-contradicting"],
        )
    ]

    report = ReportGenerator().generate(
        "inv-1", event, [supporting, contradicting], hypotheses
    )

    assert "反向证据/不确定性" in report.markdown
    assert "real supporting evidence" in report.markdown
    assert "real contradicting evidence" in report.markdown
    assert "ev-missing" not in report.markdown


def _evidence(
    evidence_id: str,
    summary: str,
    *,
    timestamp: str = "2026-07-03T14:00:00+08:00",
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime.fromisoformat(timestamp),
        summary=summary,
    )


def _hypothesis(
    *,
    cause_type: CauseType = CauseType.DEPLOYMENT_REGRESSION,
    summary: str = "A recent deployment is the likely cause.",
    confidence: float = 0.88,
    supporting_evidence_ids: list[str] | None = None,
    contradicting_evidence_ids: list[str] | None = None,
) -> Hypothesis:
    return Hypothesis(
        cause_type=cause_type,
        summary=summary,
        confidence=confidence,
        supporting_evidence_ids=supporting_evidence_ids or [],
        contradicting_evidence_ids=contradicting_evidence_ids or [],
        next_actions=["Compare with the previous deployment."],
    )
