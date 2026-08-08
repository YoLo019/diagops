from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.domain.evidence import AlertStatus, RuntimeStateValue, Severity
from backend.domain.tool_queries import (
    MemoryQuery,
    RelatedAlertQuery,
    RuntimeStateQuery,
    ScopedTelemetryQuery,
    ServiceCatalogQuery,
    TraceDirection,
    TraceQuery,
)

_START = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _scoped(**overrides):
    return {
        "entity_ids": ["checkout-1"],
        "window_start": _START,
        "window_end": _START + timedelta(minutes=30),
        **overrides,
    }


def test_scoped_query_defaults_to_empty_scope_and_limit_50():
    query = ScopedTelemetryQuery()

    assert query.entity_ids == []
    assert query.window_start is None
    assert query.window_end is None
    assert query.limit == 50


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_ids": ["bad id with spaces"]},
        {"entity_ids": ["-leading-separator"]},
        {"entity_ids": ["x" * 129]},
        {"entity_ids": [f"e{index}" for index in range(21)]},
        {"window_start": datetime(2026, 7, 20, 9, 30)},
        {"window_start": _START + timedelta(hours=1), "window_end": _START},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_scoped_query_rejects_out_of_contract_values(changes):
    with pytest.raises(ValidationError):
        ScopedTelemetryQuery(**_scoped(**changes))


@pytest.mark.parametrize("missing", ["window_start", "window_end"])
def test_scoped_query_requires_window_fields_together(missing):
    values = _scoped()
    values.pop(missing)

    with pytest.raises(ValidationError):
        ScopedTelemetryQuery(**values)


def test_trace_query_accepts_bounded_filters():
    query = TraceQuery(
        **_scoped(),
        service="checkout-service",
        operation="POST:/checkout",
        trace_id="a" * 32,
        error_only=True,
        min_duration_ms=250.5,
        direction=TraceDirection.UPSTREAM,
    )

    assert query.direction == TraceDirection.UPSTREAM
    assert query.min_duration_ms == 250.5


@pytest.mark.parametrize(
    "changes",
    [
        {"trace_id": "not-hex"},
        {"trace_id": "a" * 15},
        {"trace_id": "a" * 17},
        {"min_duration_ms": -1},
        {"min_duration_ms": 3_600_001},
        {"min_duration_ms": float("inf")},
        {"min_duration_ms": float("nan")},
        {"service": "svc with space"},
    ],
)
def test_trace_query_rejects_invalid_filters(changes):
    with pytest.raises(ValidationError):
        TraceQuery(**_scoped(**changes))


def test_runtime_state_query_bounds_states_and_uniqueness():
    query = RuntimeStateQuery(
        **_scoped(),
        states=[RuntimeStateValue.RESTARTING, RuntimeStateValue.OOM_KILLED],
        include_healthy=True,
    )

    assert len(query.states) == 2

    with pytest.raises(ValidationError):
        RuntimeStateQuery(
            **_scoped(), states=[RuntimeStateValue.RESTARTING, RuntimeStateValue.RESTARTING]
        )
    with pytest.raises(ValidationError):
        RuntimeStateQuery(**_scoped(), states=list(RuntimeStateValue) + [RuntimeStateValue.HEALTHY])


def test_related_alert_query_bounds_filters_and_uniqueness():
    query = RelatedAlertQuery(
        **_scoped(),
        severities=[Severity.CRITICAL, Severity.WARNING],
        statuses=[AlertStatus.FIRING],
    )

    assert query.severities == [Severity.CRITICAL, Severity.WARNING]

    with pytest.raises(ValidationError):
        RelatedAlertQuery(
            **_scoped(), severities=[Severity.CRITICAL, Severity.CRITICAL]
        )
    with pytest.raises(ValidationError):
        RelatedAlertQuery(
            **_scoped(),
            statuses=[AlertStatus.FIRING, AlertStatus.RESOLVED, AlertStatus.FIRING],
        )


def test_memory_query_is_structured_and_bounded():
    query = MemoryQuery(affected_entity="payment-service", failure_mechanism="timeout")

    assert query.limit == 5

    with pytest.raises(ValidationError):
        MemoryQuery(limit=11)
    with pytest.raises(ValidationError):
        MemoryQuery(service="checkout-service")
    with pytest.raises(ValidationError):
        MemoryQuery(affected_entity="x" * 129)


def test_service_catalog_query_accepts_optional_name_filter():
    query = ServiceCatalogQuery(
        start_time=_START,
        end_time=_START + timedelta(minutes=30),
        reason="验证 catalog 过滤",
        name="payment-service",
    )

    assert query.name == "payment-service"
    assert query.include_dependencies is True
