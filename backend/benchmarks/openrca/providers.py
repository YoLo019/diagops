from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, median

from backend.benchmarks.openrca.models import OpenRcaRuntimeCase
from backend.benchmarks.openrca.telemetry import (
    resolve_case_directory,
    row_component,
    row_duration,
    row_metrics,
    row_parent_span_id,
    row_span_id,
    row_success,
    row_timestamp,
    row_trace_id,
    rows_for,
)
from backend.diagnosis.signal_semantics import (
    AnomalySegment,
    SeriesPoint,
    classify_metric_signal,
    detect_anomaly_segments,
    select_balanced_evidence,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import (
    DependencyDirection,
    DependencyQuery,
    LogQuery,
    MetricQuery,
)
from backend.providers.results import ProviderResult, ProviderStatus


class _OpenRcaProvider:
    def __init__(
        self,
        dataset_root: Path,
        case: OpenRcaRuntimeCase,
        *,
        evidence_namespace: str | None = None,
    ) -> None:
        self.case = case
        self.evidence_namespace = evidence_namespace
        self.directory = resolve_case_directory(dataset_root, case.telemetry_dir)

    @staticmethod
    def _window(event: IncidentEvent, query) -> tuple[datetime, datetime, int]:
        if query is not None:
            return query.start_time, query.end_time, query.limit
        window = timedelta(minutes=event.time_window_minutes)
        return event.started_at - window, event.started_at + window, 20


class OpenRcaLogProvider(_OpenRcaProvider):
    provider = EvidenceProvider.LOG
    supported_tools = frozenset({"read_logs"})

    def collect(self, event: IncidentEvent, query: LogQuery | None = None) -> ProviderResult:
        start, end, limit = self._window(event, query)
        keywords = [item.casefold() for item in (query.keywords if query else [])]
        levels = {item.casefold() for item in (query.levels if query else [])}
        instance = query.instance if query else None
        matches: list[dict[str, str]] = []
        malformed = 0
        for row in rows_for(self.directory, "log"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            component = row_component(row)
            level = (row.get("level") or row.get("severity") or "").strip()
            message = (row.get("message") or row.get("log") or row.get("content") or "").strip()
            if timestamp is None or not message:
                malformed += 1
                continue
            row_instance = row.get("instance", "").strip()
            searchable = f"{component or ''} {message}".casefold()
            if (
                timestamp is None
                or not start <= timestamp <= end
                or (instance and row_instance != instance)
                or (levels and level.casefold() not in levels)
                or (keywords and not all(item in searchable for item in keywords))
            ):
                continue
            matches.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "component": component or "unknown",
                    "level": level,
                    "message": message[:500],
                    "instance": row_instance,
                }
            )
            if len(matches) >= limit:
                break
        if not matches:
            return _empty_result(self.provider, malformed)
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=[
                _evidence(
                    self.case,
                    self.provider,
                    EvidenceKind.LOG_PATTERN,
                    datetime.fromisoformat(match["timestamp"]),
                    f"log signal on {match['component']}",
                    _log_payload(match),
                    evidence_namespace=self.evidence_namespace,
                )
                for match in matches
            ],
            error_message=_malformed_warning(malformed),
        )


class OpenRcaMetricProvider(_OpenRcaProvider):
    provider = EvidenceProvider.METRIC
    supported_tools = frozenset({"query_metrics"})

    def collect(self, event: IncidentEvent, query: MetricQuery | None = None) -> ProviderResult:
        start, end, limit = self._window(event, query)
        # baseline 是紧邻诊断窗口、与诊断窗口等长的前置窗口（spec R2）。
        baseline_start = start - (end - start)
        requested = set(query.metric_names if query else [])
        requested_instance = query.instance if query else None
        series: dict[tuple[str, str, str], list[SeriesPoint]] = defaultdict(list)
        malformed = 0
        for row in rows_for(self.directory, "metric", "kpi"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            component = row_component(row)
            instance = row.get("instance", "").strip()
            if timestamp is None or component is None:
                malformed += 1
                continue
            metrics = list(row_metrics(row))
            if not metrics:
                malformed += 1
                continue
            for metric_name, value in metrics:
                if requested and metric_name not in requested:
                    continue
                if requested_instance and instance != requested_instance:
                    continue
                series[(component, instance, metric_name)].append(
                    SeriesPoint(timestamp=timestamp, value=value)
                )
        candidates: list[EvidenceItem] = []
        for (component, instance, metric_name), points in sorted(series.items()):
            # F10 合同：先截取本次窗口 relevant points，再统一做一次 counter
            # normalization，最后按 start 切分；窗口外 shape 不参与 counter 判定。
            relevant = [
                point for point in points if baseline_start <= point.timestamp <= end
            ]
            normalized = _normalize_counter_like(metric_name, relevant)
            baseline = [point for point in normalized if point.timestamp < start]
            observation = [
                point for point in normalized if start <= point.timestamp <= end
            ]
            for segment in detect_anomaly_segments(baseline, observation):
                candidates.append(
                    _evidence(
                        self.case,
                        self.provider,
                        EvidenceKind.METRIC_TREND,
                        segment.onset,
                        f"{metric_name} anomaly on {component}",
                        _metric_payload(component, instance, metric_name, segment),
                        evidence_namespace=self.evidence_namespace,
                    )
                )
        selected = select_balanced_evidence(candidates, limit)
        if not selected:
            return _empty_result(self.provider, malformed)
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=selected,
            error_message=_malformed_warning(malformed),
        )


class OpenRcaDependencyProvider(_OpenRcaProvider):
    provider = EvidenceProvider.DEPENDENCY
    supported_tools = frozenset({"query_dependencies"})

    def collect(self, event: IncidentEvent, query: DependencyQuery | None = None) -> ProviderResult:
        start, end, limit = self._window(event, query)
        baseline_start = start - (end - start)
        spans: dict[tuple[str, str], dict[str, object]] = {}
        malformed = 0
        for row in rows_for(self.directory, "trace", "span"):
            timestamp = row_timestamp(row, self.case.start_time.tzinfo)
            trace_id = row_trace_id(row)
            span_id = row_span_id(row)
            component = row_component(row)
            duration = row_duration(row)
            if (
                timestamp is None
                or trace_id is None
                or span_id is None
                or component is None
                or duration is None
            ):
                malformed += 1
                continue
            if not baseline_start <= timestamp <= end:
                continue
            spans[(trace_id, span_id)] = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_id": row_parent_span_id(row),
                "component": component,
                "timestamp": timestamp,
                "duration": duration,
                "success": row_success(row),
            }

        samples: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for span in spans.values():
            parent_id = span["parent_id"]
            parent = spans.get((str(span["trace_id"]), str(parent_id)))
            if parent is None:
                continue
            parent_component = str(parent["component"])
            child_component = str(span["component"])
            if parent_component == child_component:
                continue
            if (
                query
                and query.target
                and query.target
                not in {
                    parent_component,
                    child_component,
                }
            ):
                continue
            if query and query.direction == DependencyDirection.UPSTREAM:
                parent_component, child_component = child_component, parent_component
            samples[(parent_component, child_component)].append(
                {
                    "parent": parent_component,
                    "child": child_component,
                    "timestamp": span["timestamp"],
                    "duration": span["duration"],
                    "success": span["success"],
                }
            )

        candidates: list[EvidenceItem] = []
        for (parent, child), edge_samples in sorted(samples.items()):
            window_samples = [
                sample for sample in edge_samples if start <= sample["timestamp"] <= end
            ]
            if not window_samples:
                continue
            errors = [sample for sample in window_samples if not sample["success"]]
            baseline_points = [
                SeriesPoint(timestamp=sample["timestamp"], value=float(sample["duration"]))
                for sample in edge_samples
                if baseline_start <= sample["timestamp"] < start
            ]
            observation_points = [
                SeriesPoint(timestamp=sample["timestamp"], value=float(sample["duration"]))
                for sample in window_samples
            ]
            edge = {
                "parent": parent,
                "child": child,
                "latency": fmean(float(sample["duration"]) for sample in window_samples),
                "count": len(window_samples),
                "error_count": len(errors),
            }
            if baseline_points:
                edge["baseline_latency"] = median(point.value for point in baseline_points)
            segments = detect_anomaly_segments(baseline_points, observation_points)
            for segment in segments:
                candidates.append(
                    _evidence(
                        self.case,
                        self.provider,
                        EvidenceKind.DEPENDENCY_HEALTH,
                        segment.onset,
                        f"{parent} to {child} dependency edge",
                        _dependency_payload(
                            parent,
                            child,
                            {**edge, "deviation_score": segment.strength},
                            signal_type="latency",
                            signal_name="dependency_latency",
                            strength=segment.strength,
                            onset=segment.onset,
                            ended_at=segment.ended_at,
                            baseline_value=segment.baseline_value,
                            strength_basis=segment.strength_basis,
                            point_count=segment.point_count,
                        ),
                        evidence_namespace=self.evidence_namespace,
                    )
                )
            if errors:
                # 错误事件本身即异常 observation；onset 取首个错误 span。
                onset = min(sample["timestamp"] for sample in errors)
                candidates.append(
                    _evidence(
                        self.case,
                        self.provider,
                        EvidenceKind.DEPENDENCY_HEALTH,
                        onset,
                        f"{parent} to {child} dependency edge",
                        _dependency_payload(
                            parent,
                            child,
                            {**edge, "deviation_score": 0.0},
                            signal_type="timeout",
                            signal_name="dependency_errors",
                            strength=0.0,
                            onset=onset,
                            ended_at=max(sample["timestamp"] for sample in errors),
                            baseline_value=edge.get("baseline_latency"),
                        ),
                        evidence_namespace=self.evidence_namespace,
                    )
                )
        selected = select_balanced_evidence(candidates, limit)
        if not selected:
            return _empty_result(self.provider, malformed)
        return ProviderResult(
            provider=self.provider,
            status=_status(malformed),
            evidence_items=selected,
            error_message=_malformed_warning(malformed),
        )


def _is_counter_like_name(metric_name: str) -> bool:
    """仅识别明确累计命名：`total_` 前缀或 `_total`/`_count`/`_counter` 后缀。"""
    name = metric_name.casefold()
    return name.startswith("total_") or name.endswith(
        ("_total", "_count", "_counter")
    )


def _normalize_counter_like(
    metric_name: str, points: Sequence[SeriesPoint]
) -> list[SeriesPoint]:
    """把可确定的单调累计 series 转为右侧 timestamp 的 per-second rate。

    命名不符、样本不足三个、含非有限值、时间非严格递增、值下降或正向 delta
    不足两个时完整保留原 gauge 点；不猜测 reset 语义。
    """
    # ponytail: 当前只按 metric 命名与形状识别累计 counter；有可靠 metric-type
    # metadata 后应以 metadata 替换该 name+shape heuristic，且仍不猜 reset 语义。
    if not _is_counter_like_name(metric_name):
        return list(points)
    ordered = sorted(points, key=lambda point: point.timestamp)
    if len(ordered) < 3:
        return list(points)
    if any(not math.isfinite(point.value) for point in ordered):
        return list(points)
    pairs = list(zip(ordered, ordered[1:], strict=False))
    if any(right.timestamp <= left.timestamp for left, right in pairs):
        return list(points)
    deltas = [right.value - left.value for left, right in pairs]
    if any(delta < 0 for delta in deltas):
        return list(points)
    if sum(1 for delta in deltas if delta > 0) < 2:
        return list(points)
    # 前置条件已保证 elapsed 严格为正且 delta 有限非负，rate 必然有限非负。
    return [
        SeriesPoint(
            timestamp=right.timestamp,
            value=(right.value - left.value)
            / (right.timestamp - left.timestamp).total_seconds(),
        )
        for left, right in pairs
    ]


def _evidence(
    case: OpenRcaRuntimeCase,
    provider: EvidenceProvider,
    kind: EvidenceKind,
    timestamp: datetime,
    summary: str,
    payload: dict,
    *,
    evidence_namespace: str | None = None,
) -> EvidenceItem:
    fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # Evidence 使用全局主键；按 Investigation 隔离可避免配对策略写入相同内容时冲突。
    namespace = f"{evidence_namespace}:" if evidence_namespace else ""
    evidence_id = hashlib.sha256(
        f"{namespace}{case.case_id}:{provider.value}:{fingerprint}".encode()
    ).hexdigest()[:24]
    return EvidenceItem(
        id=f"ev-openrca-{evidence_id}",
        provider=provider,
        kind=kind,
        timestamp=timestamp,
        summary=summary,
        payload=payload,
    )


def _metric_payload(
    component: str, instance: str, metric_name: str, segment: AnomalySegment
) -> dict:
    payload = {
        "signal_type": classify_metric_signal(metric_name),
        "signal_name": metric_name,
        "component": component,
        "current_value": segment.peak_value,
        "baseline_value": segment.baseline_value,
        "change_percent": (
            (segment.peak_value - segment.baseline_value)
            / max(abs(segment.baseline_value), 1e-9)
            * 100
        ),
        # 兼容字段，值已归一化到 0..10，与 normalized_strength 相同。
        "deviation_score": segment.strength,
        "normalized_strength": segment.strength,
        "anomaly_onset": segment.onset.isoformat(),
        "anomaly_ended_at": segment.ended_at.isoformat(),
        "anomaly_segment_id": segment.segment_id,
        # additive 质量字段：强度口径与 segment 支持点数，供 basis-aware 排序。
        "strength_basis": segment.strength_basis,
        "anomaly_point_count": segment.point_count,
    }
    if instance:
        payload["instance"] = instance
    for key, value in _hierarchy(component).items():
        payload.setdefault(key, value)
    return payload


def _dependency_payload(
    parent: str,
    child: str,
    edge: dict,
    *,
    signal_type: str,
    signal_name: str,
    strength: float,
    onset: datetime,
    ended_at: datetime,
    baseline_value: float | None,
    strength_basis: str | None = None,
    point_count: int | None = None,
) -> dict:
    payload = {
        "edges": [edge],
        "component": child,
        "dependency": child,
        "signal_type": signal_type,
        "signal_name": signal_name,
        "current_value": edge["latency"],
        "deviation_score": strength,
        "normalized_strength": strength,
        "anomaly_onset": onset.isoformat(),
        "anomaly_ended_at": ended_at.isoformat(),
        "anomaly_segment_id": f"seg-{onset.isoformat()}",
    }
    if baseline_value is not None:
        payload["baseline_value"] = baseline_value
    if strength_basis is not None and point_count is not None:
        # additive 质量字段，与 metric adapter 同一模式（spec §8.2、F18）；仅
        # segment 派生的 latency Evidence 投影。错误事件 Evidence 非 segment
        # 派生，保持缺字段走 legacy 派生，不伪造质量字段。
        payload["strength_basis"] = strength_basis
        payload["anomaly_point_count"] = point_count
    return payload


def _hierarchy(component: str) -> dict[str, str]:
    """按公开 schema 拆分 node.service-instance 层级；原子名称保持原样。"""
    node, separator, instance = component.partition(".")
    if not separator or not node.strip() or not instance.strip():
        return {}
    service = re.sub(r"-\d+$", "", instance)
    return {
        "node": node,
        "instance": instance,
        "service": service or instance,
    }


def _exception_name(message: str) -> str | None:
    match = re.search(r"\b([A-Za-z]\w*Exception)\b", message)
    return match.group(1) if match else None


def _log_payload(match: dict[str, str]) -> dict:
    message = match["message"]
    exception = _exception_name(message)
    lowered = message.casefold()
    if "timeout" in lowered:
        signal_type = "timeout"
    elif any(item in lowered for item in ("restart", "crash", "container")):
        signal_type = "process"
    else:
        signal_type = "error"
    payload = {
        "matches": [match],
        "component": match["component"],
        "signal_type": signal_type,
        "signal_name": exception or match["level"] or "log_error",
    }
    if match["instance"]:
        payload["instance"] = match["instance"]
    if exception:
        payload["exception"] = exception
    return payload


def _status(malformed: int) -> ProviderStatus:
    return ProviderStatus.PARTIAL if malformed else ProviderStatus.SUCCESS


def _malformed_warning(malformed: int) -> str | None:
    return f"ignored {malformed} malformed telemetry rows" if malformed else None


def _empty_result(provider: EvidenceProvider, malformed: int) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=_status(malformed),
        error_message=_malformed_warning(malformed),
    )
