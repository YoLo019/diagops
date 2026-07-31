"""OpenRCA 与生产 Prometheus 共用的确定性信号语义核心。

纯函数模块：不读取文件、URL、PromQL、task、scenario 或 Ground Truth；分类顺序、
阈值与 family 优先级固定，全部来自已批准 spec，无可调参数层。counter 转换、vendor
schema 解析与有界查询由调用方 adapter 负责。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from backend.domain.evidence import EvidenceItem

_EPSILON = 1e-9
_MAX_STRENGTH = 10.0

# container 只表示作用域，不出现在任何 token 中，避免抢占 network 语义。
# packet 本身就是网络上下文：损伤词 + packet 即 corruption，普通 packet 计数归 traffic。
_NETWORK_SCOPE_TOKENS = ("network", "tcp", "udp", "socket", "packet")
_CORRUPTION_TOKENS = ("drop", "dropped", "error", "loss", "corrupt", "retransmit")
_NETWORK_LATENCY_TOKENS = ("latency", "delay", "rtt", "wait")
_NETWORK_TRAFFIC_TOKENS = ("bytes", "packets", "bandwidth", "throughput")
_TRAFFIC_TOKENS = ("qps", "request_count", "traffic")
_DISK_TOKENS = ("disk", "iops", "io_wait", "iowait")
_PROCESS_TOKENS = ("restart", "process")
_ERROR_TOKENS = ("error", "fail", "5xx")

# family 覆盖优先级固定；不在表内的 family 按名称排在最后，保证可重复。
_FAMILY_ORDER = (
    "network_corruption",
    "network_latency",
    "memory",
    "cpu",
    "disk_io",
    "process",
    "error",
    "timeout",
    "traffic",
    "latency",
)


@dataclass(frozen=True)
class SeriesPoint:
    """单个 metric series 观测点；调用方负责完成 counter 到 rate 的转换。"""

    timestamp: datetime
    value: float


@dataclass(frozen=True)
class AnomalySegment:
    """同一 series 内连续异常点的聚合；onset 为首个异常 observation。"""

    segment_id: str
    onset: datetime
    ended_at: datetime
    strength: float
    peak_value: float
    baseline_value: float
    point_count: int


def classify_metric_signal(metric_name: str) -> str:
    """按固定优先级把 metric 名称映射到 canonical signal family。"""
    name = metric_name.casefold()
    if any(token in name for token in _NETWORK_SCOPE_TOKENS):
        # 只有 dropped/error/loss/corrupt/retransmit 才是 corruption；
        # 普通 packet/byte 计数是 traffic。
        if any(token in name for token in _CORRUPTION_TOKENS):
            return "network_corruption"
        if any(token in name for token in _NETWORK_LATENCY_TOKENS):
            return "network_latency"
        if any(token in name for token in _NETWORK_TRAFFIC_TOKENS):
            return "traffic"
    if any(token in name for token in _TRAFFIC_TOKENS):
        return "traffic"
    if "memory" in name:
        return "memory"
    if "cpu" in name:
        return "cpu"
    if any(token in name for token in _DISK_TOKENS):
        return "disk_io"
    if any(token in name for token in _PROCESS_TOKENS):
        return "process"
    if any(token in name for token in _ERROR_TOKENS):
        return "error"
    return "latency"


def detect_anomaly_segments(
    baseline_points: Sequence[SeriesPoint],
    observation_points: Sequence[SeriesPoint],
) -> tuple[AnomalySegment, ...]:
    """按本 series 的 median/MAD 阈值把连续异常点聚为 segment。

    deviation 是与本 series threshold 的比率，强度封顶在 [0, 10]，保证跨 series
    可比较；baseline 为空或全部非有限时无法建立参照，返回空。
    """
    baseline_values = [point.value for point in baseline_points if math.isfinite(point.value)]
    if not baseline_values:
        return ()
    center = median(baseline_values)
    mad = median(abs(value - center) for value in baseline_values)
    threshold = max(3 * mad, abs(center) * 0.1, _EPSILON)

    ordered = sorted(
        (point for point in observation_points if math.isfinite(point.value)),
        key=lambda point: (point.timestamp, point.value),
    )
    if not ordered:
        return ()
    intervals = [
        (right.timestamp - left.timestamp).total_seconds()
        for left, right in zip(ordered, ordered[1:], strict=False)
    ]
    max_gap = 2 * median(intervals) if intervals else math.inf

    segments: list[AnomalySegment] = []
    current: list[tuple[SeriesPoint, float]] = []
    for point in ordered:
        deviation = abs(point.value - center) / threshold
        if deviation <= 1.0:
            segments.extend(_close_segment(current, center))
            current = []
            continue
        if (
            current
            and (point.timestamp - current[-1][0].timestamp).total_seconds() > max_gap
        ):
            segments.extend(_close_segment(current, center))
            current = []
        current.append((point, deviation))
    segments.extend(_close_segment(current, center))
    return tuple(segments)


def select_balanced_evidence(
    items: Sequence[EvidenceItem], limit: int
) -> list[EvidenceItem]:
    """family 均衡的有界选择：先保证每个出现的 family 覆盖一个，再 round-robin 填满。

    只在 family 内比较归一化强度；相同 (signal_type, component, onset) 的候选合并，
    保留原始 signal names 和最强有限值。
    """
    if limit <= 0:
        return []
    groups: dict[tuple[str, str, datetime], list[EvidenceItem]] = {}
    for item in items:
        key = (
            str(item.payload.get("signal_type", "")),
            str(item.payload.get("component", "")),
            item.timestamp,
        )
        groups.setdefault(key, []).append(item)
    merged = [_merge_group(group) for group in groups.values()]

    by_family: dict[str, list[EvidenceItem]] = {}
    for item in merged:
        by_family.setdefault(_family(item), []).append(item)
    for family_items in by_family.values():
        family_items.sort(key=_sort_key)
    families = sorted(by_family, key=_family_rank)

    selected: list[EvidenceItem] = []
    round_index = 0
    while len(selected) < limit:
        added = False
        for family in families:
            queue = by_family[family]
            if round_index < len(queue):
                selected.append(queue[round_index])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        round_index += 1
    return selected


def _close_segment(
    current: list[tuple[SeriesPoint, float]], center: float
) -> list[AnomalySegment]:
    if not current:
        return []
    onset = current[0][0].timestamp
    strongest_point, strongest_deviation = max(current, key=lambda entry: entry[1])
    return [
        AnomalySegment(
            segment_id=f"seg-{onset.isoformat()}",
            onset=onset,
            ended_at=current[-1][0].timestamp,
            strength=min(strongest_deviation, _MAX_STRENGTH),
            peak_value=strongest_point.value,
            baseline_value=center,
            point_count=len(current),
        )
    ]


def _merge_group(group: list[EvidenceItem]) -> EvidenceItem:
    representative = min(group, key=_sort_key)
    signal_names = sorted(
        {str(item.payload.get("signal_name", "")) for item in group} - {""}
    )
    return representative.model_copy(
        update={"payload": {**representative.payload, "signal_names": signal_names}}
    )


def _family(item: EvidenceItem) -> str:
    return str(item.payload.get("signal_type", ""))


def _family_rank(family: str) -> tuple[int, str]:
    if family in _FAMILY_ORDER:
        return (_FAMILY_ORDER.index(family), family)
    return (len(_FAMILY_ORDER), family)


def _sort_key(item: EvidenceItem) -> tuple[float, datetime, str, str, str]:
    return (
        -_strength(item),
        item.timestamp,
        str(item.payload.get("component", "")),
        str(item.payload.get("signal_name", "")),
        item.id,
    )


def _strength(item: EvidenceItem) -> float:
    value = item.payload.get("normalized_strength")
    return (
        float(value)
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        else 0.0
    )
