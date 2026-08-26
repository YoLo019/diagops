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
from typing import Literal

from backend.domain.evidence import EvidenceItem

_EPSILON = 1e-9
_MAX_STRENGTH = 10.0

# container 只表示作用域，不出现在任何 token 中，避免抢占 network 语义。
# packet 本身就是网络上下文：损伤词 + packet 即 corruption，普通 packet 计数归 traffic。
_NETWORK_SCOPE_TOKENS = ("network", "tcp", "udp", "socket", "packet")
_CORRUPTION_TOKENS = ("drop", "dropped", "error", "loss", "corrupt", "retransmit")
_NETWORK_LATENCY_TOKENS = ("latency", "delay", "rtt", "wait")
_NETWORK_TRAFFIC_TOKENS = ("bytes", "packets", "bandwidth", "throughput")
_TRAFFIC_TOKENS = ("qps", "request_count", "traffic", "workload")
_DISK_TOKENS = ("disk", "iops", "io_wait", "iowait")
_SOCKET_TOKENS = ("socket", "sockets")
_PROCESS_TOKENS = ("restart", "process")
_ERROR_TOKENS = ("error", "fail", "5xx")

# family 覆盖优先级固定；不在表内的 family 按名称排在最后，保证可重复。
_FAMILY_ORDER = (
    "network_corruption",
    "network_latency",
    "socket",
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
    """同一 series 内连续异常点的聚合；onset 为首个异常 observation。

    strength_basis 区分幅度可比较的 relative 强度与退化 baseline 下的
    presence_only（仅确认越过异常边界，strength 固定 1.0）。
    """

    segment_id: str
    onset: datetime
    ended_at: datetime
    strength: float
    peak_value: float
    baseline_value: float
    point_count: int
    strength_basis: Literal["relative", "presence_only"]


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
        if any(token in name for token in _SOCKET_TOKENS):
            return "socket"
    if any(token in name for token in _TRAFFIC_TOKENS):
        return "traffic"
    if "memory" in name or "mem" in name:
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
    # 退化 baseline 没有可用于跨 series 比较的幅度尺度：非零 observation 仍形成
    # segment，但 strength 只表示越过异常边界（spec §8.2）。
    degenerate = abs(center) <= _EPSILON and mad <= _EPSILON

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
            segments.extend(_close_segment(current, center, degenerate))
            current = []
            continue
        if (
            current
            and (point.timestamp - current[-1][0].timestamp).total_seconds() > max_gap
        ):
            segments.extend(_close_segment(current, center, degenerate))
            current = []
        current.append((point, deviation))
    segments.extend(_close_segment(current, center, degenerate))
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
        family_items[:] = _sort_family(family_items)
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
    current: list[tuple[SeriesPoint, float]], center: float, degenerate: bool
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
            strength=1.0 if degenerate else min(strongest_deviation, _MAX_STRENGTH),
            peak_value=strongest_point.value,
            baseline_value=center,
            point_count=len(current),
            strength_basis="presence_only" if degenerate else "relative",
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


def _strength_basis(item: EvidenceItem) -> str:
    """派生内存态 basis：缺字段或非法值一律 legacy，绝不写回 payload（spec F8）。"""
    value = item.payload.get("strength_basis")
    return value if value in ("relative", "presence_only") else "legacy"


def _point_count(item: EvidenceItem) -> int:
    """segment 支持点数；缺失或非法（非 int、bool、小于 1）派生安全默认 1。"""
    value = item.payload.get("anomaly_point_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _sort_family(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """family 内按 derived basis 建 relative/presence/legacy 三队列再按队首合并。

    跨 basis 竞争只比较队首共同 support（point count），再按 onset、component、
    signal name、Evidence ID；绝不比较不同 basis 的 numeric strength（spec §8.3、F6）。
    """
    queues = {
        "relative": sorted(
            (item for item in items if _strength_basis(item) == "relative"),
            key=_relative_key,
        ),
        "presence_only": sorted(
            (item for item in items if _strength_basis(item) == "presence_only"),
            key=_cross_basis_key,
        ),
        "legacy": sorted(
            (item for item in items if _strength_basis(item) == "legacy"),
            key=_sort_key,
        ),
    }
    ordered: list[EvidenceItem] = []
    while any(queues.values()):
        best_basis = min(
            (basis for basis, queue in queues.items() if queue),
            key=lambda basis: _cross_basis_key(queues[basis][0]),
        )
        ordered.append(queues[best_basis].pop(0))
    return ordered


def _relative_key(item: EvidenceItem) -> tuple[float, int, datetime, str, str, str]:
    # relative 队列：strength 降序，同 strength 时支持点数先于 onset。
    return (
        -_strength(item),
        -_point_count(item),
        item.timestamp,
        str(item.payload.get("component", "")),
        str(item.payload.get("signal_name", "")),
        item.id,
    )


def _cross_basis_key(item: EvidenceItem) -> tuple[int, datetime, str, str, str]:
    # presence 队列内部与跨 basis 队首竞争共用同一共同 support 键。
    return (
        -_point_count(item),
        item.timestamp,
        str(item.payload.get("component", "")),
        str(item.payload.get("signal_name", "")),
        item.id,
    )
