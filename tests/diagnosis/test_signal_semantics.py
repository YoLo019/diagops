"""Signal Core 的共享语义契约：分类优先级、异常段、onset 与 family 均衡选择。"""

from datetime import UTC, datetime, timedelta

import pytest

from backend.diagnosis.signal_semantics import (
    SeriesPoint,
    classify_metric_signal,
    detect_anomaly_segments,
    select_balanced_evidence,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider

BASE = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def _points(values: list[float], start: datetime = BASE, step: int = 60) -> list[SeriesPoint]:
    return [
        SeriesPoint(timestamp=start + timedelta(seconds=step * index), value=value)
        for index, value in enumerate(values)
    ]


def _evidence(
    item_id: str,
    signal_type: str,
    component: str,
    onset: datetime,
    strength: float,
    signal_name: str,
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=onset,
        summary=f"{signal_name} anomaly on {component}",
        payload={
            "signal_type": signal_type,
            "signal_name": signal_name,
            "component": component,
            "anomaly_onset": onset.isoformat(),
            "normalized_strength": strength,
        },
    )


# --- taxonomy -------------------------------------------------------------


def test_container_network_drop_is_corruption_not_process():
    assert (
        classify_metric_signal("container_network_receive_packets_dropped")
        == "network_corruption"
    )


def test_plain_packet_count_is_traffic_not_corruption():
    assert classify_metric_signal("container_network_receive_packets_total") == "traffic"


@pytest.mark.parametrize(
    "metric_name",
    [
        "node_network_transmit_errors_total",
        "network_packet_loss_ratio",
        "tcp_retransmit_segments_total",
        "network_corrupt_packets_total",
        "packet_loss",
        "node_packets_dropped_total",
    ],
)
def test_network_error_loss_corrupt_retransmit_are_corruption(metric_name):
    assert classify_metric_signal(metric_name) == "network_corruption"


@pytest.mark.parametrize(
    "metric_name",
    ["tcp_time_wait_sockets", "node_network_rtt_seconds", "network_delay_seconds"],
)
def test_tcp_wait_and_rtt_are_network_latency(metric_name):
    assert classify_metric_signal(metric_name) == "network_latency"


@pytest.mark.parametrize(
    ("metric_name", "expected"),
    [
        ("container_memory_usage_bytes", "memory"),
        ("container_cpu_usage_seconds_total", "cpu"),
        ("node_disk_read_iops", "disk_io"),
        ("node_disk_io_wait_seconds", "disk_io"),
        ("kube_pod_container_status_restarts_total", "process"),
        ("container_process_restart_count", "process"),
    ],
)
def test_resource_and_process_metrics(metric_name, expected):
    assert classify_metric_signal(metric_name) == expected


def test_container_scope_alone_does_not_imply_process():
    assert classify_metric_signal("container_start_time_seconds") != "process"


@pytest.mark.parametrize(
    ("metric_name", "expected"),
    [
        ("http_errors_total", "error"),
        ("http_request_duration_seconds", "latency"),
        ("qps", "traffic"),
        ("request_count", "traffic"),
    ],
)
def test_plain_error_latency_and_traffic(metric_name, expected):
    assert classify_metric_signal(metric_name) == expected


# --- anomaly segments -----------------------------------------------------


def test_segment_onset_ended_at_and_bounded_strength():
    baseline = _points([10.0] * 10, start=BASE - timedelta(minutes=10))
    observations = _points([10.0, 15.0, 12.5, 10.0, 30.0])

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 2
    first, second = segments
    assert first.onset == BASE + timedelta(seconds=60)
    assert first.ended_at == BASE + timedelta(seconds=120)
    assert first.point_count == 2
    # threshold = max(3*MAD, |median|*0.1, eps) = 1.0；deviation 5 封顶前为 5
    assert first.strength == 5.0
    assert first.peak_value == 15.0
    assert first.baseline_value == 10.0
    # 正常点断开后的单点异常仍形成 segment
    assert second.onset == second.ended_at == BASE + timedelta(seconds=240)
    assert second.point_count == 1


def test_zero_baseline_strength_is_clamped_to_ten():
    baseline = _points([0.0] * 10, start=BASE - timedelta(minutes=10))
    observations = _points([5.0])

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 1
    assert segments[0].strength == 10.0


def test_non_finite_points_are_ignored():
    baseline = _points(
        [10.0, float("inf"), 10.0, float("nan"), 10.0],
        start=BASE - timedelta(minutes=5),
    )
    observations = _points([float("nan"), 20.0, float("inf")])

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 1
    assert segments[0].onset == BASE + timedelta(seconds=60)


def test_sampling_gap_breaks_segment():
    baseline = _points([10.0] * 10, start=BASE - timedelta(minutes=10))
    observations = [
        SeriesPoint(timestamp=BASE, value=20.0),
        SeriesPoint(timestamp=BASE + timedelta(seconds=60), value=20.0),
        SeriesPoint(timestamp=BASE + timedelta(seconds=120), value=20.0),
        SeriesPoint(timestamp=BASE + timedelta(seconds=1200), value=20.0),
    ]

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 2
    assert segments[0].ended_at == BASE + timedelta(seconds=120)
    assert segments[1].onset == BASE + timedelta(seconds=1200)


def test_empty_baseline_or_observation_returns_no_segments():
    observations = _points([20.0])
    assert detect_anomaly_segments([], observations) == ()
    assert detect_anomaly_segments(_points([10.0] * 5), []) == ()


def test_segment_id_is_stable_and_onset_based():
    baseline = _points([10.0] * 10, start=BASE - timedelta(minutes=10))
    observations = _points([20.0])

    first = detect_anomaly_segments(baseline, observations)
    second = detect_anomaly_segments(baseline, observations)

    assert first[0].segment_id == second[0].segment_id
    assert first[0].segment_id


# --- balanced selection ---------------------------------------------------


def test_family_coverage_beats_raw_strength():
    items = [
        _evidence("ev-lat-1", "latency", "a", BASE, 10.0, "duration_a"),
        _evidence("ev-lat-2", "latency", "b", BASE, 9.0, "duration_b"),
        _evidence("ev-mem", "memory", "a", BASE, 2.0, "memory_usage"),
        _evidence("ev-corrupt", "network_corruption", "a", BASE, 1.0, "packets_dropped"),
    ]

    selected = select_balanced_evidence(items, 3)

    assert [item.id for item in selected] == ["ev-corrupt", "ev-mem", "ev-lat-1"]


def test_limit_smaller_than_family_count_uses_fixed_allowlist_order():
    items = [
        _evidence("ev-lat", "latency", "a", BASE, 10.0, "duration"),
        _evidence("ev-proc", "process", "a", BASE, 1.0, "restarts"),
        _evidence("ev-mem", "memory", "a", BASE, 1.0, "memory_usage"),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-mem", "ev-proc"]


def test_round_robin_fills_limit_after_family_coverage():
    items = [
        _evidence("ev-c1", "network_corruption", "a", BASE, 5.0, "dropped_a"),
        _evidence("ev-c2", "network_corruption", "b", BASE, 3.0, "dropped_b"),
        _evidence("ev-mem", "memory", "a", BASE, 1.0, "memory_usage"),
    ]

    selected = select_balanced_evidence(items, 3)

    assert [item.id for item in selected] == ["ev-c1", "ev-mem", "ev-c2"]


def test_same_type_component_onset_is_merged_with_signal_names():
    items = [
        _evidence("ev-weak", "memory", "a", BASE, 3.0, "memory_cache"),
        _evidence("ev-strong", "memory", "a", BASE, 5.0, "memory_usage"),
    ]

    selected = select_balanced_evidence(items, 5)

    assert [item.id for item in selected] == ["ev-strong"]
    assert selected[0].payload["signal_names"] == ["memory_cache", "memory_usage"]


def test_unknown_family_is_deterministic_after_known_families():
    items = [
        _evidence("ev-weird", "unexpected", "a", BASE, 10.0, "odd_metric"),
        _evidence("ev-mem", "memory", "a", BASE, 1.0, "memory_usage"),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-mem", "ev-weird"]


def test_selection_is_repeatable_regardless_of_input_order():
    items = [
        _evidence("ev-lat", "latency", "a", BASE, 10.0, "duration"),
        _evidence("ev-mem", "memory", "a", BASE, 2.0, "memory_usage"),
        _evidence("ev-cpu", "cpu", "a", BASE, 4.0, "cpu_usage"),
        _evidence("ev-net", "network_latency", "a", BASE, 3.0, "tcp_wait"),
    ]

    forward = select_balanced_evidence(items, 4)
    reversed_selection = select_balanced_evidence(list(reversed(items)), 4)

    assert [item.id for item in forward] == [item.id for item in reversed_selection]
    assert [item.id for item in forward] == ["ev-net", "ev-mem", "ev-cpu", "ev-lat"]


def test_empty_inputs_return_empty_selection():
    assert select_balanced_evidence([], 5) == []
    item = _evidence("ev-mem", "memory", "a", BASE, 1.0, "memory_usage")
    assert select_balanced_evidence([item], 0) == []
