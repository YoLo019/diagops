from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus
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


def test_incident_event_rejects_non_positive_time_window():
    with pytest.raises(ValidationError):
        IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="payment-service",
            environment="prod",
            severity=Severity.CRITICAL,
            title="5xx error rate increased",
            description="payment-service started returning 500 errors",
            started_at=datetime.fromisoformat("2026-07-03T14:03:00+08:00"),
            time_window_minutes=-5,
        )


@pytest.mark.parametrize("confidence", [42, -0.1, float("nan"), float("inf")])
def test_evidence_item_rejects_invalid_confidence(confidence):
    with pytest.raises(ValidationError):
        EvidenceItem(
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime.fromisoformat("2026-07-03T14:04:00+08:00"),
            summary="NullPointerException increased",
            confidence=confidence,
        )


@pytest.mark.parametrize("confidence", [42, -0.1, float("nan"), float("inf"), float("-inf")])
def test_hypothesis_rejects_invalid_confidence(confidence):
    with pytest.raises(ValidationError):
        Hypothesis(
            cause_type=CauseType.DEPLOYMENT_REGRESSION,
            summary="Deployment likely introduced the error",
            confidence=confidence,
        )


def test_evidence_item_rejects_non_json_payload_value():
    with pytest.raises(ValidationError):
        EvidenceItem(
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime.fromisoformat("2026-07-03T14:04:00+08:00"),
            summary="NullPointerException increased",
            payload={"bad": object()},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"nested": {"values": [1, float("-inf")]}},
    ],
)
def test_evidence_item_rejects_non_finite_payload_float(payload):
    with pytest.raises(ValidationError):
        EvidenceItem(
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime.fromisoformat("2026-07-03T14:04:00+08:00"),
            summary="NullPointerException increased",
            payload=payload,
        )


def test_evidence_item_defaults_to_success_status():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp="2026-07-03T15:10:00+08:00",
        summary="error spike",
    )

    assert item.status == EvidenceStatus.SUCCESS
    assert item.error_message is None


def test_provider_error_evidence_can_store_error_message():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.PROVIDER_ERROR,
        status=EvidenceStatus.FAILED,
        timestamp="2026-07-03T15:10:00+08:00",
        summary="log provider failed",
        error_message="timeout",
    )

    assert item.status == EvidenceStatus.FAILED
    assert item.error_message == "timeout"


def test_investigation_record_defaults_to_pending():
    event = IncidentEvent(
        source="webhook",
        service="checkout-service",
        environment="prod",
        severity="warning",
        title="Latency increased",
        description="checkout-service latency increased",
        started_at="2026-07-03T15:10:00+08:00",
    )
    record = InvestigationRecord(event=event)

    assert record.status == InvestigationStatus.PENDING
    assert record.failure_reason is None
    assert record.actions == []
    assert record.verification_suggestions == []
