from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.evidence import TraceSpanPayload
from backend.providers.trace_timing import with_child_timing


def span(index, start, duration, *, parent=None, service="worker", trace=1):
    return TraceSpanPayload(
        trace_id=f"{trace:032x}", span_id=f"{index:016x}",
        parent_span_id=f"{parent:016x}" if parent else None,
        service=service, operation="rpc/Call", duration_ms=duration,
        started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=start),
    )


def test_client_wait_is_covered_even_when_remote_server_is_fast():
    parent, client, server = with_child_timing([
        span(1, 0, 1000), span(2, 10, 950, parent=1),
        span(3, 100, 1, parent=2, service="remote"),
    ])
    assert parent.child_timing.covered_ms == 950
    assert parent.child_timing.uncovered_ms == 50
    assert parent.child_timing.longest_child_span_id == client.span_id
    assert parent.child_timing.longest_child_peer_service == "remote"
    assert parent.child_timing.longest_child_peer_duration_ms == 1
    assert client.child_timing is None
    assert server.child_timing is None
    assert TraceSpanPayload.model_validate(parent.model_dump()) == parent


def test_overlapping_children_are_counted_once_and_trace_ids_are_isolated():
    values = with_child_timing([
        span(1, 0, 1000), span(2, 0, 600, parent=1),
        span(3, 400, 500, parent=1), span(4, 0, 1000, parent=1, trace=2),
    ])
    assert values[0].child_timing.covered_ms == 900
    assert values[0].child_timing.uncovered_ms == 100
    assert values[0].child_timing.observed_child_count == 2


@pytest.mark.parametrize("start,duration", [(-1, 10), (990, 20)])
def test_out_of_parent_intervals_are_not_attributed(start, duration):
    parent = with_child_timing([span(1, 0, 1000), span(2, start, duration, parent=1)])[0]
    assert parent.child_timing is None


def test_duplicate_span_ids_do_not_produce_false_timing():
    values = with_child_timing([
        span(1, 0, 1000), span(2, 0, 900, parent=1), span(2, 1, 10, parent=1),
    ])
    assert values[0].child_timing is None


def test_grpc_method_survives_path_redaction_without_exposing_file_paths():
    from backend.safety.redaction import redact_value

    value = span(1, 0, 1000).model_dump()
    grpc = TraceSpanPayload.model_validate({**value, "operation": "/example.Worker/Fetch",
                                           "attributes": {"rpc.system": "grpc"}})
    assert redact_value(grpc.model_dump())["operation"] == "example.Worker/Fetch"
    path = TraceSpanPayload.model_validate({**value, "operation": "/private/config/settings"})
    assert redact_value(path.model_dump())["operation"] == "[REDACTED_PATH]"
    path = TraceSpanPayload.model_validate({**value, "operation": "/private.config/settings"})
    assert redact_value(path.model_dump())["operation"] == "[REDACTED_PATH]"


def test_ambiguous_or_different_operation_peer_is_not_claimed():
    spans = [span(1, 0, 1000), span(2, 0, 900, parent=1),
             span(3, 100, 1, parent=2, service="remote"),
             span(4, 200, 2, parent=2, service="remote")]
    assert with_child_timing(spans)[0].child_timing.longest_child_peer_service is None
    spans = spans[:3]
    spans[2] = spans[2].model_copy(update={"operation": "rpc/Other"})
    assert with_child_timing(spans)[0].child_timing.longest_child_peer_service is None
