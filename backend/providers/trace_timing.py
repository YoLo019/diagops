"""提供可复核的 span 区间事实，不推断网络、CPU 或其他故障原因。"""

from collections import defaultdict

from backend.domain.evidence import TraceChildTiming, TraceSpanPayload


def with_child_timing(spans: list[TraceSpanPayload]) -> list[TraceSpanPayload]:
    """按 trace/父 ID 计算同服务直接子 span 的覆盖时间，重叠不重复计时。"""
    children = defaultdict(dict)
    counts = defaultdict(int)
    for span in spans:
        counts[(span.trace_id, span.span_id)] += 1
        if span.parent_span_id:
            children[(span.trace_id, span.parent_span_id)][span.span_id] = span
    result = []
    for span in spans:
        intervals = []
        observed = []
        for child in children[(span.trace_id, span.span_id)].values():
            # 跨服务时钟可能偏移；服务端子 span 也不能代替调用方 RPC 等待。
            if child.service != span.service or counts[(child.trace_id, child.span_id)] != 1:
                continue
            start = (child.started_at - span.started_at).total_seconds() * 1000
            end = start + child.duration_ms
            if start < 0 or end > span.duration_ms or child.span_id == span.span_id:
                continue
            intervals.append((start, end))
            observed.append(child)
        if not observed or counts[(span.trace_id, span.span_id)] != 1:
            result.append(span.model_copy(update={"child_timing": None}))
            continue
        covered = 0.0
        right = 0.0
        for start, end in sorted(intervals):
            covered += max(0.0, end - max(start, right))
            right = max(right, end)
        longest = max(observed, key=lambda child: child.duration_ms)
        peers = [peer for peer in children[(longest.trace_id, longest.span_id)].values()
                 if peer.service != longest.service
                 and peer.operation.lstrip("/") == longest.operation.lstrip("/")
                 and counts[(peer.trace_id, peer.span_id)] == 1]
        peer = peers[0] if len(peers) == 1 else None
        result.append(span.model_copy(update={"child_timing": TraceChildTiming(
            observed_child_count=len(observed), covered_ms=round(covered, 3),
            uncovered_ms=round(max(0.0, span.duration_ms - covered), 3),
            longest_child_span_id=longest.span_id,
            longest_child_operation=longest.operation,
            longest_child_duration_ms=longest.duration_ms,
            longest_child_peer_service=peer.service if peer else None,
            longest_child_peer_duration_ms=peer.duration_ms if peer else None,
        )}))
    return result
