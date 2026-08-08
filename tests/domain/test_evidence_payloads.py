from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.evidence import (
    AlertStatus,
    RelatedAlertPayload,
    RuntimeKind,
    RuntimeStatePayload,
    RuntimeStateValue,
    Severity,
    SpanStatus,
    TraceSpanPayload,
    VerifiedIncidentPayload,
)

_NOW = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _span(**overrides):
    return {
        "trace_id": "a" * 32,
        "span_id": "1" * 16,
        "parent_span_id": None,
        "service": "checkout-service",
        "operation": "POST /checkout",
        "started_at": _NOW,
        "duration_ms": 12.5,
        "status": SpanStatus.ERROR,
        "attributes": {"http.status_code": "500"},
        **overrides,
    }


def test_trace_span_payload_round_trip():
    payload = TraceSpanPayload(**_span())

    data = payload.model_dump(mode="json")
    restored = TraceSpanPayload.model_validate(data)

    assert restored == payload


@pytest.mark.parametrize(
    "changes",
    [
        {"trace_id": "zz"},
        {"span_id": "1" * 15},
        {"parent_span_id": "2" * 32},
        {"duration_ms": -0.1},
        {"duration_ms": float("inf")},
        {"duration_ms": 3_600_001},
        {"attributes": {f"k{index}": "v" for index in range(21)}},
        {"attributes": {"x" * 65: "v"}},
        {"attributes": {"k": "v" * 257}},
        {"unknown_field": "raw-backend-json"},
    ],
)
def test_trace_span_payload_rejects_unsafe_or_unknown_fields(changes):
    with pytest.raises(ValidationError):
        TraceSpanPayload(**_span(**changes))


def test_runtime_state_payload_contract():
    payload = RuntimeStatePayload(
        entity_id="payment-2",
        runtime_kind=RuntimeKind.CONTAINER,
        state=RuntimeStateValue.OOM_KILLED,
        reason="OOMKilled",
        observed_at=_NOW,
        restart_count=3,
        ready=False,
    )

    assert payload.runtime_kind == RuntimeKind.CONTAINER

    with pytest.raises(ValidationError):
        RuntimeStatePayload(
            entity_id="payment-2",
            runtime_kind=RuntimeKind.CONTAINER,
            state=RuntimeStateValue.OOM_KILLED,
            reason="",
            observed_at=_NOW,
        )
    with pytest.raises(ValidationError):
        RuntimeStatePayload(
            entity_id="payment-2",
            runtime_kind=RuntimeKind.CONTAINER,
            state=RuntimeStateValue.OOM_KILLED,
            reason="OOMKilled",
            observed_at=_NOW,
            restart_count=-1,
        )


def test_related_alert_payload_validates_window_and_labels():
    payload = RelatedAlertPayload(
        fingerprint="fp-1",
        name="High5xx",
        entity_id="payment-service",
        severity=Severity.CRITICAL,
        status=AlertStatus.FIRING,
        starts_at=_NOW,
        labels={"team": "team-pay"},
    )

    assert payload.ends_at is None

    with pytest.raises(ValidationError):
        RelatedAlertPayload(
            fingerprint="fp-1",
            name="High5xx",
            entity_id="payment-service",
            severity=Severity.CRITICAL,
            status=AlertStatus.FIRING,
            starts_at=_NOW,
            ends_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )


def test_verified_incident_payload_never_carries_foreign_evidence_ids():
    payload = VerifiedIncidentPayload(
        source_investigation_id="inv-source",
        root_candidate_id="candidate-1",
        verified_at=_NOW,
        summary="payment timeout",
        service="checkout-service",
        environment="prod",
        affected_entity="payment-service",
        failure_mechanism="timeout",
    )

    assert "evidence_ids" not in payload.model_dump(mode="json")

    with pytest.raises(ValidationError):
        VerifiedIncidentPayload(
            source_investigation_id="inv-source",
            root_candidate_id="candidate-1",
            verified_at=_NOW,
            summary="payment timeout",
            service="checkout-service",
            environment="prod",
            foreign_evidence_ids=["ev-1"],
        )
