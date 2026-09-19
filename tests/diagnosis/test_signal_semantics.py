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
    *,
    strength_basis: str | None = None,
    point_count: int | None = None,
) -> EvidenceItem:
    payload = {
        "signal_type": signal_type,
        "signal_name": signal_name,
        "component": component,
        "anomaly_onset": onset.isoformat(),
        "normalized_strength": strength,
    }
    if strength_basis is not None:
        payload["strength_basis"] = strength_basis
    if point_count is not None:
        payload["anomaly_point_count"] = point_count
    return EvidenceItem(
        id=item_id,
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=onset,
        summary=f"{signal_name} anomaly on {component}",
        payload=payload,
    )


# --- taxonomy -------------------------------------------------------------


def test_container_network_drop_is_corruption_not_process():
    assert (
        classify_metric_signal("container_network_receive_packets_dropped")
        == "network_corruption"
    )


def test_plain_packet_count_is_traffic_not_corruption():
    assert classify_metric_signal("container_network_receive_packets_total") == "traffic"


@pytest.mark.parametrize("metric, family", [
    ("svc_container-fs-reads-bytes-total", "disk_io"),
    ("svc_container-fs-writes-total", "disk_io"),
    ("svc_container-blkio-device-usage-total", "disk_io"),
    ("svc_istio-request-total", "traffic"),
    ("svc_container-network-receive-packets-dropped-total", "network_corruption"),
    ("svc_container-memory-usage-bytes", "memory"),
])
def test_raw_container_signal_names_preserve_metric_family(metric, family):
    assert classify_metric_signal(metric) == family


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
        ("catalogue_mem", "memory"),
        ("orders_diskio", "disk_io"),
        ("payment_socket", "socket"),
        ("user_latency-50", "latency"),
        ("carts_workload", "traffic"),
    ],
)
def test_service_signal_suffixes_map_to_canonical_families(metric_name, expected):
    assert classify_metric_signal(metric_name) == expected


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
    assert first.strength_basis == "relative"
    assert first.peak_value == 15.0
    assert first.baseline_value == 10.0
    # 正常点断开后的单点异常仍形成 segment
    assert second.onset == second.ended_at == BASE + timedelta(seconds=240)
    assert second.point_count == 1
    assert second.strength_basis == "relative"


def test_zero_baseline_blip_is_presence_only_strength_one():
    # 退化 baseline（median≈0 且 MAD≈0）没有可比较的幅度尺度：只确认越过
    # 异常边界，strength 固定 1.0，不伪造幅度置信度（V10.1 spec §8.2）。
    baseline = _points([0.0] * 10, start=BASE - timedelta(minutes=10))
    observations = _points([5.0])

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 1
    segment = segments[0]
    assert segment.strength == 1.0
    assert segment.strength_basis == "presence_only"
    assert segment.point_count == 1
    assert segment.peak_value == 5.0
    assert segment.baseline_value == 0.0


def test_degenerate_baseline_sustained_anomaly_keeps_point_count():
    baseline = _points([0.0] * 10, start=BASE - timedelta(minutes=10))
    observations = _points([0.0, 3.0, 4.0, 5.0, 0.0])

    segments = detect_anomaly_segments(baseline, observations)

    assert len(segments) == 1
    segment = segments[0]
    assert segment.strength_basis == "presence_only"
    assert segment.strength == 1.0
    assert segment.point_count == 3
    assert segment.onset == BASE + timedelta(seconds=60)
    assert segment.ended_at == BASE + timedelta(seconds=180)


def test_baseline_scale_at_epsilon_boundary_selects_basis():
    # abs(median)<=eps 且 MAD<=eps 才退化；略超 eps 的常量 baseline 仍是 relative。
    degenerate = _points([1e-12] * 5, start=BASE - timedelta(minutes=5))
    scaled = _points([1e-6] * 5, start=BASE - timedelta(minutes=5))
    observations = _points([1.0])

    assert detect_anomaly_segments(degenerate, observations)[0].strength_basis == (
        "presence_only"
    )
    assert detect_anomaly_segments(scaled, observations)[0].strength_basis == "relative"


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


# --- basis-aware selection（V10.1 T5, spec §8.3） ----------------------------


def test_relative_queue_orders_by_strength_then_point_count_before_onset():
    items = [
        _evidence(
            "ev-early-weak", "memory", "a", BASE, 5.0, "mem_a",
            strength_basis="relative", point_count=1,
        ),
        _evidence(
            "ev-late-supported", "memory", "a", BASE + timedelta(minutes=5), 5.0,
            "mem_b", strength_basis="relative", point_count=3,
        ),
        _evidence(
            "ev-strongest", "memory", "a", BASE + timedelta(minutes=10), 8.0,
            "mem_c", strength_basis="relative", point_count=1,
        ),
    ]

    selected = select_balanced_evidence(items, 3)

    assert [item.id for item in selected] == [
        "ev-strongest",
        "ev-late-supported",
        "ev-early-weak",
    ]


def test_presence_queue_orders_by_point_count_then_deterministic_fields():
    items = [
        _evidence(
            "ev-single", "memory", "a", BASE, 1.0, "mem_a",
            strength_basis="presence_only", point_count=1,
        ),
        _evidence(
            "ev-sustained", "memory", "a", BASE + timedelta(minutes=5), 1.0,
            "mem_b", strength_basis="presence_only", point_count=4,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-sustained", "ev-single"]


def test_cross_basis_never_compares_numeric_strength():
    # presence 真实信号（strength 1、5 点支持）与 relative 噪声（strength 10、单点）
    # 竞争时按共同 support 决胜；绝不跨 basis 比较 numeric strength（spec F6）。
    items = [
        _evidence(
            "ev-noise", "network_corruption", "a", BASE, 10.0, "drops_noise",
            strength_basis="relative", point_count=1,
        ),
        _evidence(
            "ev-real", "network_corruption", "a", BASE + timedelta(minutes=1), 1.0,
            "drops_real", strength_basis="presence_only", point_count=5,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-real", "ev-noise"]


def test_cross_basis_point_count_tie_falls_back_to_onset_component_name_id():
    items = [
        _evidence(
            "ev-presence", "memory", "b", BASE + timedelta(minutes=1), 1.0, "mem_b",
            strength_basis="presence_only", point_count=2,
        ),
        _evidence(
            "ev-relative", "memory", "a", BASE, 7.0, "mem_a",
            strength_basis="relative", point_count=2,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    # point count 相同则按 onset、component、signal name、Evidence ID 决胜。
    assert [item.id for item in selected] == ["ev-relative", "ev-presence"]


def test_legacy_items_keep_v10_strength_first_ordering_without_writeback():
    items = [
        _evidence("ev-early", "memory", "a", BASE, 3.0, "mem_a"),
        _evidence("ev-late", "memory", "a", BASE + timedelta(minutes=5), 9.0, "mem_b"),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-late", "ev-early"]
    # legacy 只是读取 accessor 的派生内存态，绝不写回 payload。
    assert all("strength_basis" not in item.payload for item in selected)
    assert all("anomaly_point_count" not in item.payload for item in selected)


def test_legacy_and_presence_compete_on_point_count_only():
    items = [
        _evidence("ev-legacy", "memory", "a", BASE, 10.0, "mem_a"),
        _evidence(
            "ev-presence", "memory", "a", BASE + timedelta(minutes=1), 1.0, "mem_b",
            strength_basis="presence_only", point_count=3,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    # legacy 缺字段 point count 默认 1；presence 3 点支持优先。
    assert [item.id for item in selected] == ["ev-presence", "ev-legacy"]


def test_malformed_basis_derives_legacy_without_writeback():
    items = [
        _evidence(
            "ev-weird-basis", "memory", "a", BASE, 6.0, "mem_a",
            strength_basis="sometimes", point_count=5,
        ),
        _evidence("ev-legacy", "memory", "a", BASE + timedelta(minutes=1), 4.0, "mem_b"),
    ]

    selected = select_balanced_evidence(items, 2)

    # 非法 basis 派生 legacy，与缺字段 item 同队列按 V10 strength 排序。
    assert [item.id for item in selected] == ["ev-weird-basis", "ev-legacy"]
    assert selected[0].payload["strength_basis"] == "sometimes"


@pytest.mark.parametrize("bad_count", [0, -3, "5", True, 2.5])
def test_illegal_point_count_derives_default_one(bad_count):
    items = [
        _evidence(
            "ev-bad", "memory", "a", BASE, 1.0, "mem_a",
            strength_basis="presence_only", point_count=bad_count,
        ),
        _evidence(
            "ev-good", "memory", "a", BASE + timedelta(minutes=1), 1.0, "mem_b",
            strength_basis="presence_only", point_count=2,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-good", "ev-bad"]


def test_family_coverage_and_limit_unchanged_with_basis_fields():
    items = [
        _evidence(
            "ev-lat", "latency", "a", BASE, 10.0, "duration",
            strength_basis="relative", point_count=1,
        ),
        _evidence(
            "ev-mem", "memory", "a", BASE, 2.0, "memory_usage",
            strength_basis="relative", point_count=1,
        ),
        _evidence(
            "ev-proc", "process", "a", BASE, 1.0, "restarts",
            strength_basis="presence_only", point_count=4,
        ),
    ]

    selected = select_balanced_evidence(items, 2)

    assert [item.id for item in selected] == ["ev-mem", "ev-proc"]


def test_merge_group_still_dedupes_with_basis_fields():
    items = [
        _evidence(
            "ev-a", "memory", "a", BASE, 1.0, "mem_x",
            strength_basis="presence_only", point_count=2,
        ),
        _evidence(
            "ev-b", "memory", "a", BASE, 1.0, "mem_y",
            strength_basis="presence_only", point_count=2,
        ),
    ]

    selected = select_balanced_evidence(items, 5)

    assert len(selected) == 1
    assert selected[0].payload["signal_names"] == ["mem_x", "mem_y"]


def test_basis_aware_selection_is_repeatable_regardless_of_input_order():
    items = [
        _evidence(
            "ev-rel", "memory", "a", BASE, 5.0, "mem_a",
            strength_basis="relative", point_count=2,
        ),
        _evidence(
            "ev-pres", "memory", "b", BASE + timedelta(minutes=1), 1.0, "mem_b",
            strength_basis="presence_only", point_count=3,
        ),
        _evidence(
            "ev-legacy", "memory", "c", BASE + timedelta(minutes=2), 4.0, "mem_c",
        ),
        _evidence(
            "ev-cpu", "cpu", "a", BASE, 6.0, "cpu_a",
            strength_basis="relative", point_count=1,
        ),
    ]

    forward = select_balanced_evidence(items, 4)
    reversed_selection = select_balanced_evidence(list(reversed(items)), 4)

    assert [item.id for item in forward] == [item.id for item in reversed_selection]
