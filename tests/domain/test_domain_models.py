from datetime import datetime

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.reports import IncidentReport


def test_incident_event_defaults_time_window():
    event = IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="payment-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="5xx error rate increased",
        description="payment-service started returning 500 errors",
        started_at=datetime.fromisoformat("2026-07-03T14:03:00+08:00"),
    )

    assert event.time_window_minutes == 30
    assert event.signals == {}


def test_evidence_item_keeps_structured_payload():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime.fromisoformat("2026-07-03T14:04:00+08:00"),
        summary="NullPointerException increased",
        payload={"exception": "NullPointerException", "count": 120},
    )

    assert item.payload["count"] == 120


def test_hypothesis_links_supporting_and_contradicting_evidence():
    hypothesis = Hypothesis(
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="Deployment likely introduced the error",
        confidence=0.84,
        supporting_evidence_ids=["ev-log-1", "ev-deploy-1"],
        contradicting_evidence_ids=["ev-dependency-1"],
        next_actions=["Review recent deployment", "Consider rollback"],
    )

    assert hypothesis.confidence == 0.84
    assert hypothesis.supporting_evidence_ids == ["ev-log-1", "ev-deploy-1"]


def test_report_contains_markdown_and_hypotheses():
    report = IncidentReport(
        investigation_id="inv-1",
        summary="Likely deployment regression",
        timeline=[{"time": "14:00", "event": "deployment"}],
        hypotheses=[],
        markdown="# RCA Report",
    )

    assert report.markdown == "# RCA Report"
