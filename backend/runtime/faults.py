from __future__ import annotations

from collections.abc import Mapping

M2_FAULT_POINTS: frozenset[str] = frozenset(
    {
        "before_phase_start",
        "provider_before_commit",
        "tool_after_commit_before_checkpoint",
        "model_after_send",
        "persistence_mid_transaction",
        "lease_lost",
        "concurrent_resume",
        "checkpoint_tamper",
        "parallel_session_failure",
        "cancel_parallel_specialists",
        "unsafe_event_payload",
    }
)

# 保留 Milestone 2 的 deferred 集合用于追踪原始验收缺口。
DEFERRED_M3_FAULT_POINTS: frozenset[str] = frozenset(
    {"sse_reconnect", "otel_unavailable", "replay_external_call"}
)
CONNECTED_M3_FAULT_POINTS: frozenset[str] = frozenset(
    {"sse_reconnect", "otel_unavailable", "replay_external_call"}
)
APPROVED_FAULT_POINTS = M2_FAULT_POINTS | CONNECTED_M3_FAULT_POINTS


class RuntimeInjectedFault(RuntimeError):
    pass


class NoFaultInjector:
    """生产容器唯一允许的 fault injector；所有 hook 都是不可配置的 no-op。"""

    def hit(self, point: str) -> None:
        if point not in APPROVED_FAULT_POINTS:
            raise ValueError(f"unknown runtime fault point: {point}")


class DeterministicFaultInjector:
    """仅供测试按第 N 次命中触发确定性故障。"""

    def __init__(self, failures: Mapping[str, int]) -> None:
        unknown = set(failures) - APPROVED_FAULT_POINTS
        if unknown:
            raise ValueError(f"unknown runtime fault points: {sorted(unknown)}")
        if any(hit < 1 for hit in failures.values()):
            raise ValueError("fault hit number must be positive")
        self._failures = dict(failures)
        self._hits: dict[str, int] = {}

    def hit(self, point: str) -> None:
        if point not in APPROVED_FAULT_POINTS:
            raise ValueError(f"unknown runtime fault point: {point}")
        hit = self._hits.get(point, 0) + 1
        self._hits[point] = hit
        if self._failures.get(point) == hit:
            raise RuntimeInjectedFault(f"injected runtime fault: {point}")
